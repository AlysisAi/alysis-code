from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..approval_scope import exact_file_set_scope, exact_verify_command_set_scope
from ..assets import AssetAlreadyExistsError, AssetError, asset_reference_check
from ..assets.plan_binding import bind_asset_to_matching_tasks
from ..assets.surface import (
    AssetSurfaceDetail,
    AssetSurfaceEntry,
    build_asset_surface,
)
from ..diff_paths import parse_patch_changed_files
from ..execution_shared import (
    build_task_execution_instruction_bundle,
    build_task_local_workspace_reporting_diff,
    capture_task_local_workspace_baseline,
    cleanup_task_local_workspace_baseline,
    resolve_managed_task_step_budget,
    safe_task_file_component,
    write_execution_budget_artifact,
    write_execution_context_artifact,
)
from ..forge import (
    ForgeError,
    RunPaths,
    append_planner_chat,
    append_planner_summary,
    append_transcript_note,
    create_plan_run,
    ensure_execution_dirs,
    ensure_workspace_context_artifacts,
    finalize_plan,
    find_task,
    load_plan,
    make_run_paths,
    now_iso,
    save_plan,
    set_task_status,
    write_task_report,
)
from ..git_ops import head_commit
from ..knowledge_librarian import prepare_planner_knowledge, resolve_knowledge_workspace_root
from ..plan_assistant import apply_guarded_planner_plan_update, run_planner_turn
from ..plan_reconciliation import reconcile_plan_with_workspace
from ..plan_repair import PlannerRepairReport, apply_plan_status, record_plan_repair
from ..plan_validation import validate_plan
from ..review_gate import ReviewError, review_task
from ..runtime_kind import RuntimeKind
from ..sandbox_doctor import diagnose_sandbox
from ..surface.types import ApprovalRequest
from ..task_scope import check_scope
from ..verify_gate import (
    resolve_verify_commands,
    run_task_verification,
    verify_run_result_to_payload,
)
from .protocol import ProtocolError, redact_secrets

DEFAULT_MAX_DIFF_BYTES = 64 * 1024
MAX_DIFF_BYTES = 1024 * 1024
MAX_LISTED_PLANS = 100
MAX_FORGE_ASSET_BYTES = 25 * 1024 * 1024
PLAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
REAL_EXECUTE_AUTO_UNSUPPORTED_REASON = (
    "Forge Execute v1 is review-mode only. Auto and fullaccess execution remain disabled "
    "until they have separate approval, sandbox, and cancellation coverage."
)
SUPPORTED_SANDBOX_PROFILES = frozenset({"default", "strict", "warn", "off"})
REAL_EXECUTE_SUPPORTED_MODES = frozenset({"review"})


def append_planner_router_event(paths: RunPaths, payload: dict[str, Any]) -> None:
    """Persist legacy planner-router telemetry when an older planner supplies it.

    The deterministic planner router was removed from the current runtime, so
    production planner results no longer include this payload.  Keeping the
    writer local to the compatibility bridge lets older IDE requests and test
    doubles remain readable without restoring the removed router to Forge.
    """
    paths.notes_dir.mkdir(parents=True, exist_ok=True)
    event_type = (
        "planner_router_failure"
        if str(payload.get("fallback_reason") or "").strip()
        else "planner_router_decision"
    )
    event = {"type": event_type, "ts": now_iso(), "payload": payload}
    event_path = paths.notes_dir / "planner_router_events.jsonl"
    with event_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )


def _throw_if_cancelled(cancellation_token: Any | None) -> None:
    if cancellation_token is None:
        return
    throw_if_cancelled = getattr(cancellation_token, "throw_if_cancelled", None)
    if callable(throw_if_cancelled):
        throw_if_cancelled("cancelled_by_user")
        return
    if bool(getattr(cancellation_token, "is_cancelled", False)):
        raise RuntimeError("cancelled_by_user")


def _call_accepts_keyword(callable_obj: Callable[..., Any], keyword: str) -> bool:
    try:
        sig = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in sig.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
            and parameter.name == keyword
        ):
            return True
    return False


FORGE_REGISTRY_REJECTED_MESSAGE = "Forge persisted plan registry path was rejected."
SAFE_IDE_TASK_STATUSES = frozenset(
    {
        "planned",
        "in_progress",
        "blocked",
        "done",
        "failed",
        "changes_requested",
        "superseded",
    }
)
MAX_PLAN_EDIT_TEXT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class ForgePlanRecord:
    session_id: str
    plan_id: str
    paths: RunPaths
    status: str = "planned"
    job_id: str | None = None
    source: str = "active_memory"
    warnings: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiffRecord:
    diff_id: str
    session_id: str
    plan_id: str
    job_id: str | None
    path: Path
    rel_path: str
    file_path: str
    status: str
    old_label: str
    new_label: str
    size_bytes: int


def create_ide_forge_plan(
    *,
    session_id: str,
    workspace_root: Path,
    instruction: str,
    cfg: Any,
    planner_runner: Callable[..., Any] | None = None,
    cancellation_token: Any | None = None,
) -> tuple[ForgePlanRecord, dict[str, Any]]:
    clean_instruction = instruction.strip()
    if not clean_instruction:
        raise ProtocolError("missing_field", "instruction is required.")
    _throw_if_cancelled(cancellation_token)
    quality: dict[str, Any]
    try:
        paths = create_plan_run(workspace_root)
        plan = load_plan(paths)
        append_transcript_note(paths, role="user", message=clean_instruction)
        append_planner_chat(paths, role="user", message=clean_instruction)
        workspace_context = _workspace_context_payload(paths)
        relevant_knowledge_section = _planner_knowledge_section(
            paths=paths,
            plan=plan,
            instruction=clean_instruction,
        )
        planner = planner_runner or run_planner_turn
        planner_kwargs = {
            "cfg": cfg,
            "api_key_override": None,
            "plan": plan,
            "transcript_tail": [{"role": "user", "content": clean_instruction}],
            "workspace_context": workspace_context,
            "user_text": clean_instruction,
            "stream": False,
            "relevant_knowledge_section": relevant_knowledge_section,
            "prefer_context": "forge",
            "awaiting_clarification": False,
            "pending_questions": [],
            "run_paths": paths,
        }
        if _call_accepts_keyword(planner, "cancellation_token"):
            planner_kwargs["cancellation_token"] = cancellation_token
        planner_result = planner(**planner_kwargs)
        _throw_if_cancelled(cancellation_token)
        assistant_message = str(getattr(planner_result, "assistant_message", "") or "")
        if assistant_message:
            append_transcript_note(paths, role="assistant", message=assistant_message)
            append_planner_chat(paths, role="assistant", message=assistant_message)
        router_event = getattr(planner_result, "planner_router_event", None)
        if isinstance(router_event, dict):
            append_planner_router_event(paths, router_event)

        planner_error = str(getattr(planner_result, "error", "") or "").strip()
        if planner_error:
            append_planner_summary(paths, f"planner error: {planner_error}")
            save_plan(paths, plan)
            raise ProtocolError(
                "forge_plan_failed",
                str(redact_secrets(f"Forge planner failed: {planner_error}")),
            )

        raw_plan_update = getattr(planner_result, "plan_update", None)
        if not isinstance(raw_plan_update, dict) or not raw_plan_update:
            questions = [
                str(question).strip()
                for question in getattr(planner_result, "questions", []) or []
                if str(question).strip()
            ]
            if questions:
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Planner questions: {'; '.join(questions)}",
                )
            append_planner_summary(paths, "planner produced no structured task update")
            save_plan(paths, plan)
            raise ProtocolError(
                "forge_plan_incomplete",
                "Forge planner did not produce a structured execution plan. No shallow fallback plan was created.",
            )

        apply_result = apply_guarded_planner_plan_update(
            plan,
            raw_plan_update,
            latest_user_text=clean_instruction,
            workspace_context=workspace_context,
        )
        reconciliation_warnings: list[str] = []
        if apply_result.changed:
            target_ids = set(apply_result.added_task_ids + apply_result.updated_task_ids) or None
            reconciliation = reconcile_plan_with_workspace(
                plan,
                workspace_root=paths.root,
                workspace_context=workspace_context,
                user_text=clean_instruction,
                transcript_tail=[
                    {"role": "user", "content": clean_instruction},
                    {"role": "assistant", "content": assistant_message},
                ],
                target_task_ids=target_ids,
            )
            reconciliation_warnings = list(getattr(reconciliation, "warnings", []) or [])
        resolved_commands = _resolved_verification_commands(cfg, paths.root)
        if resolved_commands:
            plan["ide_verification_commands"] = list(resolved_commands)
        if _plan_tasks(plan):
            finalize_plan(plan)
        record_plan_repair(plan, getattr(planner_result, "repair", None) or PlannerRepairReport())
        apply_plan_status(plan, validation_warnings=validate_plan(plan))
        save_plan(paths, plan)
        quality = validate_ide_plan_quality(
            plan,
            verification_commands=resolved_commands,
            extra_warnings=[
                *list(getattr(apply_result, "warnings", []) or []),
                *reconciliation_warnings,
            ],
        )
        _write_plan_validation_artifact(paths=paths, warnings=quality["warnings"])
    except ProtocolError:
        raise
    except ForgeError as e:
        raise ProtocolError("forge_plan_failed", str(e)) from e
    except Exception as e:
        _throw_if_cancelled(cancellation_token)
        raise ProtocolError(
            "forge_plan_failed",
            str(redact_secrets(f"Forge planner adapter failed: {e}")),
        ) from e

    if not _plan_tasks(plan):
        raise ProtocolError(
            "forge_plan_incomplete",
            "Forge planner did not produce executable tasks. No shallow fallback plan was returned.",
        )

    record = ForgePlanRecord(
        session_id=session_id,
        plan_id=paths.run_id,
        paths=paths,
        source="active_memory",
        warnings=tuple(quality["warnings"]),
        verification_commands=tuple(resolved_commands),
    )
    return record, plan


def load_recorded_plan(record: ForgePlanRecord, *, migrate_legacy: bool = True) -> dict[str, Any]:
    try:
        validate_forge_run_artifact_root(record)
        return load_plan(record.paths, migrate_legacy=migrate_legacy)
    except ForgeError as e:
        raise ProtocolError("forge_plan_not_found", str(e)) from e


def forge_plan_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    created_session: bool,
    verification_commands: list[str] | None = None,
) -> dict[str, Any]:
    root_name = forge_artifact_root_name(record.plan_id)
    commands = (
        verification_commands
        if verification_commands is not None
        else list(record.verification_commands)
        or _string_list(plan.get("ide_verification_commands"))
    )
    quality = validate_ide_plan_quality(
        plan,
        verification_commands=commands,
        extra_warnings=list(record.warnings),
    )
    return {
        "plan_id": record.plan_id,
        "session_id": record.session_id,
        "job_id": record.job_id,
        "status": record.status,
        "source": record.source,
        "created_session": created_session,
        "project_goal": str(redact_secrets(str(plan.get("project_goal") or ""))),
        "summary": str(redact_secrets(str(plan.get("summary") or ""))),
        "warnings": quality["warnings"],
        "incomplete": quality["incomplete"],
        "tasks": [
            _task_to_ide_schema(
                task,
                verification_commands=commands,
                warnings=quality["task_warnings"].get(str(task.get("id") or ""), []),
            )
            for task in _plan_tasks(plan)
        ],
        "artifacts": [
            {
                "kind": "forge_plan_json",
                "artifact_id": f"{root_name}:plan/plan.json",
                "path": "plan/plan.json",
            },
            {
                "kind": "forge_plan_markdown",
                "artifact_id": f"{root_name}:plan/PLAN.md",
                "path": "plan/PLAN.md",
            },
        ],
        "plan_artifact_id": f"{root_name}:plan/plan.json",
        "plan_markdown_artifact_id": f"{root_name}:plan/PLAN.md",
    }


def forge_status_result(record: ForgePlanRecord, plan: dict[str, Any]) -> dict[str, Any]:
    payload = forge_plan_result(record, plan, created_session=False)
    payload["diff_count"] = len(list_diff_records([record]))
    return payload


def forge_show_result(record: ForgePlanRecord, plan: dict[str, Any], *, cfg: Any) -> dict[str, Any]:
    payload = forge_status_result(record, plan)
    payload["assets"] = _asset_entries_payload(record, cfg=cfg, include_deleted=True)
    payload["legacy_assets"] = _legacy_plan_assets_payload(plan)
    payload["artifact_count"] = len(payload.get("artifacts") or [])
    return payload


def forge_plan_state_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    cfg: Any,
) -> dict[str, Any]:
    payload = forge_show_result(record, plan, cfg=cfg)
    payload["ide_revision"] = _plan_ide_revision(plan)
    payload["assistant"] = _plan_assistant_payload(plan)
    payload["goal"] = str(redact_secrets(str(plan.get("project_goal") or "")))
    payload["validation"] = forge_plan_validate_result(record, plan)
    return payload


@dataclass(frozen=True, slots=True)
class ForgePlanEditOutcome:
    """A committed plan edit, separated from the response payload rendering.

    Apply functions mutate and persist the plan under the caller's lock;
    forge_plan_edit_render builds the (filesystem-heavy) response payload and
    must run outside bridge-wide locks.
    """

    changed: bool
    previous_revision: int
    changed_fields: tuple[str, ...]
    task: dict[str, Any] | None = None


def forge_plan_set_assistant_apply(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    instruction: str,
    expected_revision: Any = None,
) -> ForgePlanEditOutcome:
    previous_revision = _check_expected_revision(plan, expected_revision)
    clean = _safe_plan_edit_text(instruction, field="instruction")
    previous = _plan_assistant_payload(plan).get("instruction", "")
    changed = previous != clean
    if changed:
        plan["ide_assistant"] = {
            "instruction": clean,
            "updated_at": now_iso(),
            "source": "ide_protocol",
        }
        plan["ide_revision"] = previous_revision + 1
        save_plan(record.paths, plan)
    return ForgePlanEditOutcome(
        changed=changed,
        previous_revision=previous_revision,
        changed_fields=("assistant.instruction",) if changed else (),
    )


def forge_plan_set_goal_apply(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    goal: str,
    expected_revision: Any = None,
) -> ForgePlanEditOutcome:
    previous_revision = _check_expected_revision(plan, expected_revision)
    clean = _safe_plan_edit_text(goal, field="goal")
    if not clean:
        raise ProtocolError("missing_field", "goal must be a non-empty string.")
    previous = str(plan.get("project_goal") or "")
    changed = previous != clean
    if changed:
        plan["project_goal"] = clean
        plan["ide_revision"] = previous_revision + 1
        plan["ide_stale_reason"] = "goal_changed"
        save_plan(record.paths, plan)
    return ForgePlanEditOutcome(
        changed=changed,
        previous_revision=previous_revision,
        changed_fields=("project_goal",) if changed else (),
    )


def forge_plan_update_task_apply(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    task_id: str,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    expected_revision: Any = None,
) -> ForgePlanEditOutcome:
    clean_task_id = _required_task_id(task_id)
    previous_revision = _check_expected_revision(plan, expected_revision)
    try:
        task = find_task(plan, clean_task_id)
    except ForgeError as e:
        raise ProtocolError("task_not_found", str(redact_secrets(str(e)))) from e

    changed_fields: list[str] = []
    if title is not None:
        clean_title = _safe_plan_edit_text(title, field="title")
        if not clean_title:
            raise ProtocolError("missing_field", "task title must be a non-empty string.")
        if str(task.get("title") or "") != clean_title:
            task["title"] = clean_title
            changed_fields.append("title")
    if body is not None:
        clean_body = _safe_plan_edit_text(body, field="body")
        if str(task.get("description") or "") != clean_body:
            task["description"] = clean_body
            changed_fields.append("description")
    if status is not None:
        clean_status = str(status or "").strip().lower()
        if clean_status not in SAFE_IDE_TASK_STATUSES:
            raise ProtocolError(
                "invalid_task_status",
                "task status must be one of: " + ", ".join(sorted(SAFE_IDE_TASK_STATUSES)),
            )
        if str(task.get("status") or "") != clean_status:
            set_task_status(plan, clean_task_id, clean_status)
            changed_fields.append("status")

    if changed_fields:
        plan["ide_revision"] = previous_revision + 1
        plan["ide_stale_reason"] = "task_updated"
        save_plan(record.paths, plan)
    task_payload = _task_to_ide_schema(
        task,
        verification_commands=_string_list(plan.get("ide_verification_commands")),
        warnings=[],
    )
    return ForgePlanEditOutcome(
        changed=bool(changed_fields),
        previous_revision=previous_revision,
        changed_fields=tuple(changed_fields),
        task=task_payload,
    )


def forge_plan_edit_render(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    cfg: Any,
    outcome: ForgePlanEditOutcome,
) -> dict[str, Any]:
    result = _plan_edit_result(
        record,
        plan,
        cfg=cfg,
        changed=outcome.changed,
        previous_revision=outcome.previous_revision,
        changed_fields=list(outcome.changed_fields),
    )
    if outcome.task is not None:
        result["task"] = outcome.task
    return result


def forge_plan_set_assistant_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    instruction: str,
    expected_revision: Any = None,
    cfg: Any,
) -> dict[str, Any]:
    outcome = forge_plan_set_assistant_apply(
        record, plan, instruction=instruction, expected_revision=expected_revision
    )
    return forge_plan_edit_render(record, plan, cfg=cfg, outcome=outcome)


def forge_plan_set_goal_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    goal: str,
    expected_revision: Any = None,
    cfg: Any,
) -> dict[str, Any]:
    outcome = forge_plan_set_goal_apply(
        record, plan, goal=goal, expected_revision=expected_revision
    )
    return forge_plan_edit_render(record, plan, cfg=cfg, outcome=outcome)


def forge_plan_update_task_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    task_id: str,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    expected_revision: Any = None,
    cfg: Any,
) -> dict[str, Any]:
    outcome = forge_plan_update_task_apply(
        record,
        plan,
        task_id=task_id,
        title=title,
        body=body,
        status=status,
        expected_revision=expected_revision,
    )
    return forge_plan_edit_render(record, plan, cfg=cfg, outcome=outcome)


def forge_plan_validate_result(record: ForgePlanRecord, plan: dict[str, Any]) -> dict[str, Any]:
    commands = list(record.verification_commands) or _string_list(
        plan.get("ide_verification_commands")
    )
    quality = validate_ide_plan_quality(plan, verification_commands=commands)
    return {
        "ok": not bool(quality["warnings"]) and not bool(quality["incomplete"]),
        "warnings": quality["warnings"],
        "incomplete": quality["incomplete"],
        "task_warnings": quality["task_warnings"],
        "ide_revision": _plan_ide_revision(plan),
        "stale_reason": str(redact_secrets(str(plan.get("ide_stale_reason") or ""))) or None,
    }


@dataclass(slots=True)
class ForgePlanRegenerateComputation:
    """Planner output computed from a plan snapshot, awaiting a revision-checked commit."""

    plan: dict[str, Any]
    base_revision: int
    changed: bool
    quality: dict[str, Any]


def ensure_expected_plan_revision(plan: dict[str, Any], expected_revision: Any) -> int:
    return _check_expected_revision(plan, expected_revision)


def forge_plan_regenerate_compute(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    instruction: str | None = None,
    focus: str | None = None,
    expected_revision: Any = None,
    cfg: Any,
    planner_runner: Callable[..., Any] | None = None,
    cancellation_token: Any | None = None,
) -> ForgePlanRegenerateComputation:
    """Run the planner against a plan snapshot without persisting anything.

    Callers must not hold bridge-wide locks across this call: the planner is a
    provider round-trip. The snapshot is mutated in place and only becomes
    durable through forge_plan_regenerate_commit, which re-checks the revision.
    """
    old_revision = _check_expected_revision(plan, expected_revision)
    clean_instruction = (
        _safe_plan_edit_text(instruction, field="instruction") if instruction is not None else ""
    )
    clean_focus = _safe_plan_edit_text(focus, field="focus") if focus is not None else ""
    user_text = _regeneration_instruction(plan, instruction=clean_instruction, focus=clean_focus)
    before_fingerprint = _plan_regeneration_fingerprint(plan)
    _throw_if_cancelled(cancellation_token)
    try:
        append_transcript_note(record.paths, role="user", message=user_text)
        append_planner_chat(record.paths, role="user", message=user_text)
        workspace_context = _workspace_context_payload(record.paths)
        relevant_knowledge_section = _planner_knowledge_section(
            paths=record.paths,
            plan=plan,
            instruction=user_text,
        )
        planner = planner_runner or run_planner_turn
        planner_kwargs: dict[str, Any] = {
            "cfg": cfg,
            "api_key_override": None,
            "plan": plan,
            "transcript_tail": [{"role": "user", "content": user_text}],
            "workspace_context": workspace_context,
            "user_text": user_text,
            "stream": False,
            "relevant_knowledge_section": relevant_knowledge_section,
            "prefer_context": "forge",
            "awaiting_clarification": False,
            "pending_questions": [],
            "run_paths": record.paths,
        }
        if _call_accepts_keyword(planner, "cancellation_token"):
            planner_kwargs["cancellation_token"] = cancellation_token
        planner_result = planner(**planner_kwargs)
        _throw_if_cancelled(cancellation_token)
        assistant_message = str(getattr(planner_result, "assistant_message", "") or "")
        if assistant_message:
            append_transcript_note(record.paths, role="assistant", message=assistant_message)
            append_planner_chat(record.paths, role="assistant", message=assistant_message)
        router_event = getattr(planner_result, "planner_router_event", None)
        if isinstance(router_event, dict):
            append_planner_router_event(record.paths, router_event)

        planner_error = str(getattr(planner_result, "error", "") or "").strip()
        if planner_error:
            append_planner_summary(record.paths, f"planner regenerate error: {planner_error}")
            raise ProtocolError(
                "forge_plan_failed",
                str(redact_secrets(f"Forge planner regeneration failed: {planner_error}")),
            )
        raw_plan_update = getattr(planner_result, "plan_update", None)
        if not isinstance(raw_plan_update, dict) or not raw_plan_update:
            append_planner_summary(
                record.paths, "planner regeneration produced no structured update"
            )
            raise ProtocolError(
                "forge_plan_incomplete",
                "Forge planner regeneration did not produce a structured plan update.",
            )

        apply_result = apply_guarded_planner_plan_update(
            plan,
            raw_plan_update,
            latest_user_text=user_text,
            workspace_context=workspace_context,
        )
        target_ids = set(apply_result.added_task_ids + apply_result.updated_task_ids) or None
        reconciliation = reconcile_plan_with_workspace(
            plan,
            workspace_root=record.paths.root,
            workspace_context=workspace_context,
            user_text=user_text,
            transcript_tail=[
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_message},
            ],
            target_task_ids=target_ids,
        )
        _throw_if_cancelled(cancellation_token)
        resolved_commands = _resolved_verification_commands(cfg, record.paths.root)
        if resolved_commands:
            plan["ide_verification_commands"] = list(resolved_commands)
        if _plan_tasks(plan):
            finalize_plan(plan)
        after_fingerprint = _plan_regeneration_fingerprint(plan)
        changed = before_fingerprint != after_fingerprint
        quality = validate_ide_plan_quality(
            plan,
            verification_commands=resolved_commands
            or list(record.verification_commands)
            or _string_list(plan.get("ide_verification_commands")),
            extra_warnings=[
                *list(getattr(apply_result, "warnings", []) or []),
                *list(getattr(reconciliation, "warnings", []) or []),
            ],
        )
    except ProtocolError:
        raise
    except ForgeError as e:
        raise ProtocolError("forge_plan_failed", str(redact_secrets(str(e)))) from e
    except Exception as e:
        _throw_if_cancelled(cancellation_token)
        raise ProtocolError(
            "forge_plan_failed",
            str(redact_secrets(f"Forge planner regeneration adapter failed: {e}")),
        ) from e
    return ForgePlanRegenerateComputation(
        plan=plan,
        base_revision=old_revision,
        changed=changed,
        quality=quality,
    )


def forge_plan_regenerate_commit(
    record: ForgePlanRecord,
    computation: ForgePlanRegenerateComputation,
) -> None:
    """Persist a computed regeneration after re-checking the plan revision.

    Callers must serialize this against other plan writers (the bridge holds
    its state lock). A revision moved by a concurrent writer during compute
    fails closed with stale_plan_revision and persists nothing.
    """
    current = load_recorded_plan(record, migrate_legacy=False)
    current_revision = _plan_ide_revision(current)
    if current_revision != computation.base_revision:
        raise ProtocolError(
            "stale_plan_revision",
            f"Plan revision is {current_revision}; regeneration was computed from "
            f"revision {computation.base_revision}. Refresh plan state and retry.",
        )
    plan = computation.plan
    if computation.changed:
        plan["ide_revision"] = computation.base_revision + 1
        plan["ide_stale_reason"] = "regenerated"
        plan["ide_regenerated_at"] = now_iso()
    apply_plan_status(plan, validation_warnings=computation.quality["warnings"])
    try:
        save_plan(record.paths, plan)
        _write_plan_validation_artifact(
            paths=record.paths, warnings=computation.quality["warnings"]
        )
    except ForgeError as e:
        raise ProtocolError("forge_plan_failed", str(redact_secrets(str(e)))) from e


def forge_plan_regenerate_render(
    record: ForgePlanRecord,
    computation: ForgePlanRegenerateComputation,
    *,
    cfg: Any,
) -> dict[str, Any]:
    plan = computation.plan
    result = forge_plan_state_result(record, plan, cfg=cfg)
    result.update(
        {
            "old_revision": computation.base_revision,
            "new_revision": _plan_ide_revision(plan),
            "changed": computation.changed,
            "summary": str(redact_secrets(str(plan.get("summary") or ""))),
            "warnings": computation.quality["warnings"],
            "validation": forge_plan_validate_result(record, plan),
            "redacted": True,
            "secret_values_included": False,
            "audit": {
                "changed_fields": ["plan"] if computation.changed else [],
                "previous_revision": computation.base_revision,
                "revision": _plan_ide_revision(plan),
                "source": "ide_protocol",
                "operation": "forge.plan.regenerate",
                "secret_values_included": False,
            },
        }
    )
    return result


def forge_plan_regenerate_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    instruction: str | None = None,
    focus: str | None = None,
    expected_revision: Any = None,
    cfg: Any,
    planner_runner: Callable[..., Any] | None = None,
    cancellation_token: Any | None = None,
) -> dict[str, Any]:
    computation = forge_plan_regenerate_compute(
        record,
        plan,
        instruction=instruction,
        focus=focus,
        expected_revision=expected_revision,
        cfg=cfg,
        planner_runner=planner_runner,
        cancellation_token=cancellation_token,
    )
    forge_plan_regenerate_commit(record, computation)
    return forge_plan_regenerate_render(record, computation, cfg=cfg)


def _plan_edit_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    cfg: Any,
    changed: bool,
    previous_revision: int,
    changed_fields: list[str],
) -> dict[str, Any]:
    payload = forge_plan_state_result(record, plan, cfg=cfg)
    payload["changed"] = bool(changed)
    payload["audit"] = {
        "changed_fields": _redacted_strings(changed_fields),
        "previous_revision": previous_revision,
        "revision": _plan_ide_revision(plan),
        "source": "ide_protocol",
        "secret_values_included": False,
    }
    return payload


def forge_review_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    task_id: str,
    cfg: Any,
    reviewer: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    clean_task_id = _required_task_id(task_id)
    validate_forge_run_artifact_root(record)
    try:
        task = find_task(plan, clean_task_id)
    except ForgeError as e:
        raise ProtocolError("task_not_found", str(redact_secrets(str(e)))) from e
    try:
        outcome = (reviewer or review_task)(
            paths=record.paths,
            plan=plan,
            task=task,
            cfg=cfg,
            api_key_override=None,
        )
    except ReviewError as e:
        raise ProtocolError("forge_review_failed", str(redact_secrets(str(e)))) from e
    except Exception as e:  # noqa: BLE001
        raise ProtocolError(
            "forge_review_failed",
            str(redact_secrets(f"Forge review failed: {e}")),
        ) from e
    review_json = _read_json_artifact(outcome.json_path, root=record.paths.root)
    review_markdown = _read_text_artifact(outcome.markdown_path, root=record.paths.root)
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "task_id": str(redact_secrets(outcome.task_id)),
        "approved": bool(outcome.approved),
        "confidence": str(redact_secrets(outcome.confidence)),
        "summary": str(redact_secrets(outcome.summary)),
        "blocking_issues_count": int(outcome.blocking_issues_count),
        "non_blocking_issues_count": int(outcome.non_blocking_issues_count),
        "review_json": redact_secrets(review_json),
        "review_markdown": str(redact_secrets(review_markdown)),
        "json_artifact_id": f"{forge_artifact_root_name(record.plan_id)}:{_repo_rel(record.paths.root, outcome.json_path)}",
        "markdown_artifact_id": f"{forge_artifact_root_name(record.plan_id)}:{_repo_rel(record.paths.root, outcome.markdown_path)}",
        "requires_human_approval": not bool(outcome.approved),
        "action": (
            None
            if outcome.approved
            else {
                "kind": "review_needed",
                "message": "Forge review returned blocking issues; inspect the structured review before continuing.",
            }
        ),
    }


def forge_attach_result(
    record: ForgePlanRecord,
    *,
    source_path: Path,
    cfg: Any,
    title: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    return forge_assets_add_result(
        record,
        source_path=source_path,
        title=title,
        description=description,
        pinned=False,
        wait=False,
        link=True,
        cfg=cfg,
        command="forge.attach",
    )


def forge_assets_list_result(
    record: ForgePlanRecord,
    *,
    cfg: Any,
    include_deleted: bool = False,
) -> dict[str, Any]:
    assets = _asset_entries_payload(record, cfg=cfg, include_deleted=include_deleted)
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "run_id": record.paths.run_id,
        "assets": assets,
        "count": len(assets),
        "include_deleted": bool(include_deleted),
    }


def forge_assets_show_result(record: ForgePlanRecord, *, asset_id: str, cfg: Any) -> dict[str, Any]:
    surface = _asset_surface(record, cfg=cfg)
    try:
        detail = surface.show_asset(_required_asset_id(asset_id))
    except AssetError as e:
        raise ProtocolError("asset_not_found", str(redact_secrets(str(e)))) from e
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "asset": _asset_detail_payload(detail),
    }


def forge_assets_add_result(
    record: ForgePlanRecord,
    *,
    source_path: Path,
    title: str | None,
    description: str = "",
    pinned: bool = False,
    wait: bool = False,
    link: bool = False,
    cfg: Any,
    command: str = "forge.assets.add",
) -> dict[str, Any]:
    source = _resolve_workspace_asset_source(record, source_path)
    clean_title = str(title or "").strip() or source.name
    if not clean_title:
        raise ProtocolError("missing_field", "title is required.")
    surface = _asset_surface(record, cfg=cfg)
    try:
        result = surface.add_asset(
            source,
            title=clean_title,
            description=str(description or ""),
            pinned=bool(pinned),
            added_by={"phase": "ide_protocol", "command": command},
            comprehend="sync" if wait else "skip",
            dedupe_policy="link" if link else "reject",
        )
        bound_task_ids = _bind_asset_to_plan(record, surface=surface, asset_record=result.record)
        detail = surface.show_asset(result.record.id)
    except AssetAlreadyExistsError as e:
        raise ProtocolError(
            "asset_already_exists",
            str(redact_secrets(f"{e} existing_id={e.existing_id}")),
        ) from e
    except AssetError as e:
        raise ProtocolError("asset_error", str(redact_secrets(str(e)))) from e
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "asset": _asset_detail_payload(detail),
        "bound_task_ids": _redacted_strings(bound_task_ids),
        "comprehension_record": (
            redact_secrets(result.comprehension_record.to_dict())
            if result.comprehension_record is not None
            else None
        ),
        "status": "added",
    }


def forge_assets_delete_result(
    record: ForgePlanRecord, *, asset_id: str, cfg: Any
) -> dict[str, Any]:
    clean_asset_id = _required_asset_id(asset_id)
    surface = _asset_surface(record, cfg=cfg)
    try:
        deleted = surface.delete_asset(clean_asset_id)
    except AssetError as e:
        raise ProtocolError("asset_error", str(redact_secrets(str(e)))) from e
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "asset": redact_secrets(deleted.to_dict()),
        "status": "deleted",
    }


def forge_assets_edit_result(
    record: ForgePlanRecord,
    *,
    asset_id: str,
    cfg: Any,
    title: str | None = None,
    description: str | None = None,
    pinned: bool | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    clean_asset_id = _required_asset_id(asset_id)
    if title is None and description is None and pinned is None and not refresh:
        raise ProtocolError(
            "missing_field",
            "forge.assets.edit requires title, description, pinned, or refresh=true.",
        )
    surface = _asset_surface(record, cfg=cfg)
    try:
        detail = surface.edit_asset(
            clean_asset_id,
            title=title,
            description=description,
            pinned=pinned,
            retrigger_comprehension=False,
        )
        comprehension = (
            surface.refresh_comprehension(clean_asset_id, mode="sync").join() if refresh else None
        )
        if refresh:
            detail = surface.show_asset(clean_asset_id)
    except AssetError as e:
        raise ProtocolError("asset_error", str(redact_secrets(str(e)))) from e
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "asset": _asset_detail_payload(detail),
        "comprehension_record": (
            redact_secrets(comprehension.to_dict()) if comprehension is not None else None
        ),
        "status": "updated",
    }


def forge_assets_refresh_result(
    record: ForgePlanRecord,
    *,
    asset_id: str,
    cfg: Any,
) -> dict[str, Any]:
    clean_asset_id = _required_asset_id(asset_id)
    surface = _asset_surface(record, cfg=cfg)
    try:
        handle = surface.refresh_comprehension(clean_asset_id, mode="sync")
        comprehension = handle.join()
        detail = surface.show_asset(clean_asset_id)
    except AssetError as e:
        raise ProtocolError("asset_error", str(redact_secrets(str(e)))) from e
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "asset": _asset_detail_payload(detail),
        "comprehension_record": (
            redact_secrets(comprehension.to_dict()) if comprehension is not None else None
        ),
        "status": detail.comprehension_status,
        "async_background": False,
    }


def forge_assets_cancel_pending_result(record: ForgePlanRecord, *, cfg: Any) -> dict[str, Any]:
    surface = _asset_surface(record, cfg=cfg)
    cancelled = surface.cancel_pending_comprehensions()
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "cancelled_count": int(cancelled),
        "status": "cancelled" if cancelled else "idle",
        "message": "No persistent IDE background comprehensions are running."
        if not cancelled
        else "Pending in-process comprehensions were asked to stop.",
    }


def forge_assets_check_plan_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    cfg: Any,
) -> dict[str, Any]:
    surface = _asset_surface(record, cfg=cfg)
    try:
        report = asset_reference_check(plan, surface)
    except AssetError as e:
        raise ProtocolError("asset_error", str(redact_secrets(str(e)))) from e
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "deleted_referenced": [
            {"task_id": str(redact_secrets(task_id)), "asset_id": str(redact_secrets(asset_id))}
            for task_id, asset_id in report.deleted_referenced
        ],
        "missing_referenced": [
            {"task_id": str(redact_secrets(task_id)), "asset_id": str(redact_secrets(asset_id))}
            for task_id, asset_id in report.missing_referenced
        ],
        "pinned_added": _redacted_strings(list(report.pinned_added)),
        "ok": not report.deleted_referenced and not report.missing_referenced,
    }


def forge_assets_prune_legacy_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    cfg: Any,
    yes: bool,
) -> dict[str, Any]:
    if _plan_schema_version(plan) < 2:
        raise ProtocolError(
            "legacy_prune_unavailable",
            "Legacy asset pruning requires plan schema_version=2 after migration.",
        )
    surface = _asset_surface(record, cfg=cfg)
    legacy_files = _legacy_asset_files(record.paths)
    known_hashes = {
        str(asset.sha256)
        for asset in surface.index.records(include_deleted=True)
        if str(asset.sha256 or "").strip()
    }
    verified: list[str] = []
    unverified: list[str] = []
    for file_path in legacy_files:
        resolved = _resolve_existing_file(
            file_path,
            workspace_root=record.paths.root.resolve(),
            run_root=record.paths.run_dir.resolve(),
            not_found_code="legacy_asset_not_found",
        )
        digest = _sha256_file(resolved)
        rel = _repo_rel(record.paths.root, resolved)
        if digest in known_hashes:
            verified.append(rel)
        else:
            unverified.append(rel)
    payload: dict[str, Any] = {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "verified": _redacted_strings(verified),
        "unverified": _redacted_strings(unverified),
        "deleted": [],
        "requires_confirmation": bool(verified) and not yes,
        "blocked": bool(unverified),
    }
    if unverified or not verified or not yes:
        return payload
    deleted: list[str] = []
    verified_set = set(verified)
    for file_path in legacy_files:
        rel = _repo_rel(record.paths.root, file_path)
        if rel not in verified_set:
            continue
        try:
            file_path.unlink()
        except OSError as e:
            raise ProtocolError(
                "legacy_prune_failed",
                str(redact_secrets(f"Failed to delete {rel}: {e}")),
            ) from e
        deleted.append(rel)
    _remove_empty_legacy_dirs(record.paths)
    payload["deleted"] = _redacted_strings(deleted)
    payload["requires_confirmation"] = False
    payload["blocked"] = False
    return payload


def forge_execute_preview_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    task_ids: list[str],
    execution_mode: str,
    workspace_trusted: bool | None,
    sandbox_profile: str,
    max_steps: Any = None,
    no_log: bool = False,
    cfg: Any | None = None,
) -> dict[str, Any]:
    selected_tasks = _select_preview_tasks(plan, task_ids)
    commands = list(record.verification_commands) or _string_list(
        plan.get("ide_verification_commands")
    )
    mode = execution_mode.strip().lower() or "review"
    if mode not in {"readonly", "review", "auto"}:
        raise ProtocolError(
            "invalid_mode", "Forge execute preview mode must be readonly, review, or auto."
        )

    sandbox = _sandbox_preview(sandbox_profile, cfg=cfg)
    workspace_trust_required = mode in {"review", "auto"}
    file_scopes = [_preview_file_scope(task) for task in selected_tasks]
    verification = [_preview_verification(task, commands) for task in selected_tasks]
    real_supported = mode in REAL_EXECUTE_SUPPORTED_MODES
    known_risks = _preview_known_risks(selected_tasks, real_execution_supported=real_supported)
    missing = _preview_missing_prerequisites(
        selected_tasks=selected_tasks,
        verification=verification,
        workspace_trust_required=workspace_trust_required,
        workspace_trusted=workspace_trusted,
        sandbox=sandbox,
        sandbox_required=mode in {"review", "auto"},
    )
    required_approvals = _preview_required_approvals(
        mode=mode,
        selected_tasks=selected_tasks,
        verification=verification,
    )
    preview_ready = not missing
    unsupported_reason = "" if real_supported else REAL_EXECUTE_AUTO_UNSUPPORTED_REASON
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "selected_task_ids": [str(task.get("id") or "") for task in selected_tasks],
        "execution_mode_requested": mode,
        "workspace_trust_required": workspace_trust_required,
        "workspace_trusted": workspace_trusted,
        "estimated_file_scopes": file_scopes,
        "verification_commands": verification,
        "required_approvals": required_approvals,
        "runtime_approval_requirements": _preview_runtime_approval_requirements(mode),
        "approval_scopes_safe": _approval_scopes_are_safe(required_approvals),
        "sandbox_profile": sandbox,
        "known_risks": _redacted_strings(known_risks),
        "missing_prerequisites": _redacted_strings(missing),
        "preview_ready": preview_ready,
        "real_execution_supported": real_supported,
        "unsupported_reason": str(redact_secrets(unsupported_reason)),
        "active_cancellation_supported": True,
        "cancellation": {
            "supported": True,
            "kind": "cooperative_checkpoint",
            "hard_interrupt": False,
        },
        "max_steps": _optional_positive_int(max_steps),
        "no_log": bool(no_log),
        "subagents_supported": False,
        "subagents_enabled": False,
        "subagents_policy": "disabled_for_ide_forge_execute_v1",
        "next_recommended_action": _preview_next_action(
            preview_ready=preview_ready,
            missing_prerequisites=missing,
            real_execution_supported=real_supported,
        ),
        "status": "preview",
    }


def list_persisted_forge_plans(
    *,
    workspace_root: Path,
    session_id: str | None = None,
    active_records: list[ForgePlanRecord] | None = None,
    max_items: Any = MAX_LISTED_PLANS,
) -> dict[str, Any]:
    limit = _bounded_plan_count(max_items)
    active_by_plan = {record.plan_id: record for record in active_records or []}
    plans: list[dict[str, Any]] = []
    for paths, plan in _iter_persisted_plans(workspace_root, limit=limit * 2):
        active = active_by_plan.get(paths.run_id)
        source = "active_memory" if active is not None else "persisted"
        plans.append(_plan_summary(paths=paths, plan=plan, session_id=session_id, source=source))
    plans.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {
        "workspace_root": str(workspace_root),
        "plans": plans[:limit],
        "truncated": len(plans) > limit,
        "max_items": limit,
    }


def open_persisted_forge_plan(
    *,
    workspace_root: Path,
    session_id: str,
    plan_id: str,
    source: str = "loaded_persisted",
) -> tuple[ForgePlanRecord, dict[str, Any]]:
    paths = _paths_for_persisted_plan(workspace_root=workspace_root, plan_id=plan_id)
    try:
        _validate_forge_run_paths(paths, require_plan_json=True)
        plan = load_plan(paths)
    except ForgeError as e:
        raise ProtocolError("forge_plan_not_found", str(e)) from e
    return (
        ForgePlanRecord(
            session_id=session_id,
            plan_id=paths.run_id,
            paths=paths,
            source=source,
        ),
        plan,
    )


def forge_artifact_root_name(plan_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in plan_id)
    return f"forge_{safe}"


def validate_forge_run_artifact_root(record: ForgePlanRecord) -> Path:
    _validate_forge_run_paths(record.paths, require_plan_json=True)
    return record.paths.run_dir


def list_diff_records(records: list[ForgePlanRecord]) -> list[DiffRecord]:
    diffs: list[DiffRecord] = []
    for record in records:
        patches_dir = _validated_forge_patches_dir(record)
        if patches_dir is None:
            continue
        run_root = _resolve_existing_directory(record.paths.run_dir, code="forge_registry_rejected")
        workspace_root = record.paths.root.expanduser().resolve()
        for path in sorted(patches_dir.glob("*.diff"), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(workspace_root)
                resolved.relative_to(run_root)
                resolved.relative_to(patches_dir)
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            rel_path = resolved.relative_to(run_root).as_posix()
            try:
                preview = _read_limited_text(path, max_bytes=DEFAULT_MAX_DIFF_BYTES).text
            except ProtocolError:
                continue
            changed_files = parse_patch_changed_files(preview)
            file_path = _diff_file_label(changed_files)
            status = "truncated" if size > DEFAULT_MAX_DIFF_BYTES else "available"
            diffs.append(
                DiffRecord(
                    diff_id=_diff_id(record, rel_path),
                    session_id=record.session_id,
                    plan_id=record.plan_id,
                    job_id=record.job_id,
                    path=path,
                    rel_path=rel_path,
                    file_path=str(redact_secrets(file_path)),
                    status=status,
                    old_label="before",
                    new_label="after",
                    size_bytes=size,
                )
            )
    return diffs


def diff_list_result(records: list[ForgePlanRecord]) -> dict[str, Any]:
    diffs = list_diff_records(records)
    return {
        "diffs": [
            {
                "diff_id": record.diff_id,
                "session_id": record.session_id,
                "plan_id": record.plan_id,
                "job_id": record.job_id,
                "file_path": record.file_path,
                "status": record.status,
                "old_label": record.old_label,
                "new_label": record.new_label,
                "size_bytes": record.size_bytes,
            }
            for record in diffs
        ],
        "empty_reason": (
            "No Forge diff artifacts are available for this scoped plan."
            if records and not diffs
            else None
        ),
    }


def diff_get_result(
    records: list[ForgePlanRecord],
    *,
    diff_id: str,
    max_bytes: Any = DEFAULT_MAX_DIFF_BYTES,
) -> dict[str, Any]:
    clean = diff_id.strip()
    if not clean:
        raise ProtocolError("missing_field", "diff_id is required.")
    max_bytes_int = _bounded_diff_bytes(max_bytes)
    matching = [record for record in list_diff_records(records) if record.diff_id == clean]
    if not matching:
        raise ProtocolError("diff_not_found", "Diff was not found.")
    record = matching[0]
    limited = _read_limited_text(record.path, max_bytes=max_bytes_int)
    return {
        "diff_id": record.diff_id,
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "job_id": record.job_id,
        "file_path": record.file_path,
        "old_text": None,
        "new_text": None,
        "old_artifact_id": None,
        "new_artifact_id": None,
        "unified_diff": redact_secrets(limited.text),
        "truncated": limited.truncated,
        "size_bytes": record.size_bytes,
        "max_bytes": max_bytes_int,
        "redaction": "protocol_preview",
    }


def forge_execute_review_job_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    task_ids: list[str],
    workspace_trusted: bool | None,
    sandbox_profile: str,
    cfg: Any,
    surface: Any,
    job_id: str,
    max_steps: Any = None,
    no_log: bool = False,
    agent_runner: Callable[..., int] | None = None,
    cancellation_token: Any | None = None,
) -> dict[str, Any]:
    _throw_if_cancelled(cancellation_token)
    if not task_ids:
        raise ProtocolError("missing_field", "forge.execute requires explicit task_ids.")
    if not str(getattr(cfg, "model", "") or "").strip():
        raise ProtocolError("config_error", "Model is not set.")
    preview = forge_execute_preview_result(
        record,
        plan,
        task_ids=task_ids,
        execution_mode="review",
        workspace_trusted=workspace_trusted,
        sandbox_profile=sandbox_profile,
        max_steps=max_steps,
        no_log=no_log,
        cfg=cfg,
    )
    if not preview["real_execution_supported"]:
        raise ProtocolError(
            "forge_execute_unsupported",
            preview["unsupported_reason"] or REAL_EXECUTE_AUTO_UNSUPPORTED_REASON,
        )
    if not preview["approval_scopes_safe"]:
        raise ProtocolError(
            "forge_execute_prerequisites_failed",
            "Forge Execute requires exact safe approval scopes.",
        )
    if not preview["preview_ready"]:
        blockers = "; ".join(str(item) for item in preview["missing_prerequisites"])
        raise ProtocolError(
            "forge_execute_prerequisites_failed",
            str(redact_secrets(blockers or "Forge Execute prerequisites are not satisfied.")),
        )

    selected_tasks = _select_preview_tasks(plan, task_ids)
    dependency_blockers = _selection_dependency_blockers(plan, selected_tasks)
    if dependency_blockers:
        raise ProtocolError(
            "forge_execute_prerequisites_failed",
            str(redact_secrets("; ".join(dependency_blockers))),
        )

    validate_forge_run_artifact_root(record)
    ensure_execution_dirs(record.paths)
    sandbox_cfg = _cfg_for_sandbox_profile(cfg, sandbox_profile.strip().lower() or "default")
    fallback_commands = list(record.verification_commands) or _string_list(
        plan.get("ide_verification_commands")
    )
    results: list[dict[str, Any]] = []
    all_success = True
    completed_task_ids = _initial_completed_task_ids(plan)
    surface.emit_status_update(mode="review", model=getattr(cfg, "model", None))
    surface.emit_info(f"forge_execute_started {record.plan_id}")
    for task in selected_tasks:
        _throw_if_cancelled(cancellation_token)
        dependency_blockers = _runtime_dependency_blockers(
            plan=plan,
            task=task,
            completed_task_ids=completed_task_ids,
        )
        if dependency_blockers:
            result = _block_review_task(
                record=record,
                plan=plan,
                task=task,
                summary="; ".join(dependency_blockers),
                surface=surface,
            )
        else:
            result = _run_single_review_task(
                record=record,
                plan=plan,
                task=task,
                cfg=sandbox_cfg,
                surface=surface,
                fallback_verify_commands=fallback_commands,
                max_steps=max_steps,
                no_log=no_log,
                agent_runner=agent_runner,
                cancellation_token=cancellation_token,
            )
        _throw_if_cancelled(cancellation_token)
        results.append(result)
        if result["status"] == "done":
            completed_task_ids.add(str(task.get("id") or "").strip())
        else:
            all_success = False
    save_plan(record.paths, plan)
    diff_count = len(list_diff_records([record]))
    status = "completed" if all_success else "failed"
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "job_id": job_id,
        "status": status,
        "selected_task_ids": [str(task.get("id") or "") for task in selected_tasks],
        "task_results": results,
        "diff_count": diff_count,
        "max_steps": _optional_positive_int(max_steps),
        "no_log": bool(no_log),
        "subagents_supported": False,
        "subagents_enabled": False,
        "subagents_policy": "disabled_for_ide_forge_execute_v1",
        "active_cancellation_supported": True,
    }


def _run_single_review_task(
    *,
    record: ForgePlanRecord,
    plan: dict[str, Any],
    task: dict[str, Any],
    cfg: Any,
    surface: Any,
    fallback_verify_commands: list[str],
    max_steps: Any,
    no_log: bool,
    agent_runner: Callable[..., int] | None,
    cancellation_token: Any | None = None,
) -> dict[str, Any]:
    _throw_if_cancelled(cancellation_token)
    task_id = str(task.get("id") or "").strip()
    title = str(task.get("title") or "").strip() or task_id
    paths = record.paths
    safe_task = safe_task_file_component(task_id)
    verify_commands = _verification_commands_for_task(task, fallback_verify_commands)
    allowed_scope = _task_write_scope_for_execution(task)
    started_at = now_iso()
    patch_path = paths.execution_patches_dir / f"{safe_task}.diff"
    verify_path = paths.execution_verify_dir / f"{safe_task}.txt"
    surface.emit_plan_node_updated(task_id, "in_progress", title)
    surface.emit_swarm_worker_state_changed(task_id, "running", role="forge_execute_review")

    task_step_budget = resolve_managed_task_step_budget(
        cfg=cfg,
        plan=plan,
        task=task,
        kind="ide_forge_execute",
        mode="review",
        verification_enabled=bool(verify_commands),
        max_steps_override=_optional_positive_int(max_steps),
        attempt_count=_task_attempt_count(task),
        image_count=0,
    )
    instruction_bundle = build_task_execution_instruction_bundle(
        plan=plan,
        task=task,
        root=paths.root,
        cfg=cfg,
        role_model=str(getattr(cfg, "model", "") or ""),
        mode="review",
        yes=False,
        deny_write_prefixes=[".alysis"],
        allow_write_globs=allowed_scope,
        non_interactive=True,
        verification_enabled=bool(verify_commands),
        authoritative_verification_commands=verify_commands or None,
        api_key=None,
        subagents_enabled=False,
    )
    context_path = write_execution_context_artifact(
        run_paths=paths,
        task_id=task_id,
        context_text=instruction_bundle.artifact_text,
    )
    budget_payload = instruction_bundle.to_budget_artifact_payload()
    budget_payload["step_budget"] = task_step_budget.to_payload()
    budget_path = write_execution_budget_artifact(
        run_paths=paths,
        task_id=task_id,
        payload=budget_payload,
    )
    baseline = capture_task_local_workspace_baseline(
        paths.root, before_commit=head_commit(paths.root)
    )
    run_code = 1
    run_error: str | None = None
    verify_summary: str | None = None
    verify_payload: dict[str, Any] | None = None
    try:
        runner = agent_runner
        if runner is None:
            from ..agent_loop import run_agent as runner
        try:
            runner_kwargs = {
                "cfg": cfg,
                "root": paths.root,
                "instruction": instruction_bundle.instruction,
                "mode": "review",
                "runtime_kind": RuntimeKind.FORGE_EXEC,
                "yes": False,
                "max_steps": task_step_budget.resolved_max_steps,
                "no_log": no_log,
                "api_key_override": None,
                "console": None,
                "surface": surface,
                "image_paths": list(instruction_bundle.image_paths) or None,
                "deny_write_prefixes": [".alysis"],
                "allow_write_globs": allowed_scope,
                "non_interactive": True,
                "session_log_dir_override": paths.execution_sessions_dir,
                "session_id_override": safe_task,
                "usage_role": f"ide_forge_execute:{task_id}",
                "enable_compaction": False,
                "enable_tool_output_offload": True,
                "enable_conversation_summarization": True,
                "compaction_profile": "execution",
                "enable_chat_turn_step_budget": False,
                "one_shot_execution": True,
                "verification_enabled": bool(verify_commands),
                "authoritative_verification_commands": verify_commands or None,
                "subagents_enabled": False,
            }
            if _call_accepts_keyword(runner, "cancellation_token"):
                runner_kwargs["cancellation_token"] = cancellation_token
            run_code = int(runner(**runner_kwargs) or 0)
            _throw_if_cancelled(cancellation_token)
        except Exception as exc:  # noqa: BLE001
            _throw_if_cancelled(cancellation_token)
            run_error = str(redact_secrets(f"agent failed: {exc}"))
            run_code = 1
        after_commit = head_commit(paths.root)
        diff = build_task_local_workspace_reporting_diff(
            paths.root,
            baseline=baseline,
            after_commit=after_commit,
        )
    finally:
        cleanup_task_local_workspace_baseline(baseline)

    patch_path.write_text(diff.patch_text, encoding="utf-8")
    changed_files = list(diff.changed_files)
    success = run_code == 0
    status = "done"
    summary_parts: list[str] = []
    if run_error:
        success = False
        summary_parts.append(run_error)
    elif run_code != 0:
        success = False
        summary_parts.append(f"agent exited non-zero ({run_code})")
    if diff.inspection_error:
        success = False
        summary_parts.append(str(redact_secrets(diff.inspection_error)))
    scope_ok, scope_violations = check_scope(changed_files, allowed_scope, root=paths.root)
    if not scope_ok:
        success = False
        preview = ", ".join(scope_violations[:20])
        if len(scope_violations) > 20:
            preview += ", ..."
        summary_parts.append(
            f"out-of-scope file changes detected ({len(scope_violations)}): {preview}"
        )
    if success:
        verify_allowed = _request_verify_approval(surface, verify_commands)
        if not verify_allowed:
            success = False
            status = "verify_failed"
            verify_summary = "verification approval denied"
            summary_parts.append(verify_summary)
            surface.emit_verify_gate_result(
                _verification_command_label(verify_commands),
                False,
                verify_summary,
                worker_id=task_id,
                role="forge_execute_review",
            )
        else:
            verify_result = run_task_verification(
                root=paths.root,
                commands=verify_commands,
                artifact_path=verify_path,
                cfg=cfg,
            )
            verify_summary = str(redact_secrets(verify_result.summary))
            verify_payload = redact_secrets(
                verify_run_result_to_payload(root=paths.root, result=verify_result)
            )
            surface.emit_verify_gate_result(
                _verification_command_label(verify_commands),
                bool(verify_result.all_passed),
                verify_summary,
                worker_id=task_id,
                role="forge_execute_review",
            )
            if not verify_result.all_passed:
                success = False
                status = "verify_failed"
                summary_parts.append(f"verification failed: {verify_summary}")
    if not success and status == "done":
        status = "failed"
    if success:
        summary = "IDE Forge Execute review task completed with scoped artifacts and passing verification."
        surface.emit_review_gate_decision(
            "accepted",
            summary,
            worker_id=task_id,
            role="forge_execute_review",
        )
    else:
        summary = "; ".join(summary_parts) or "IDE Forge Execute review task failed."
        surface.emit_review_gate_decision(
            "blocked",
            summary,
            worker_id=task_id,
            role="forge_execute_review",
        )
    finished_at = now_iso()
    report_path = write_task_report(
        paths=paths,
        task=task,
        result="success" if success else "failure",
        result_kind="ide_review_execute",
        summary=str(redact_secrets(summary)),
        started_at=started_at,
        finished_at=finished_at,
        changed_files=changed_files,
        verify_commands=verify_commands,
        patch_path=patch_path,
        budget_artifact_path=budget_path,
        execution_log_artifacts=None,
        verify_artifact_path=verify_path if verify_path.exists() else None,
        verify_summary=verify_summary,
        verify_payload=verify_payload,
        verify_command_source="ide_forge_execute",
        merge_result="not merged: IDE review-mode execution leaves changes in the workspace",
    )
    set_task_status(plan, task_id, status)
    save_plan(paths, plan)
    surface.emit_plan_node_updated(task_id, status, title)
    surface.emit_swarm_worker_state_changed(
        task_id, "completed" if success else "failed", role="forge_execute_review"
    )
    if not success:
        surface.emit_error(
            "forge_execute_task_failed",
            summary,
            False,
            worker_id=task_id,
            role="forge_execute_review",
        )
    return {
        "task_id": str(redact_secrets(task_id)),
        "status": status,
        "success": success,
        "summary": str(redact_secrets(summary)),
        "changed_files": _redacted_strings(changed_files),
        "report_artifact_id": f"{forge_artifact_root_name(record.plan_id)}:{_repo_rel(paths.root, report_path)}",
        "patch_artifact_id": f"{forge_artifact_root_name(record.plan_id)}:{_repo_rel(paths.root, patch_path)}",
        "context_artifact_id": f"{forge_artifact_root_name(record.plan_id)}:{_repo_rel(paths.root, context_path)}",
        "verify_summary": verify_summary,
    }


def _block_review_task(
    *,
    record: ForgePlanRecord,
    plan: dict[str, Any],
    task: dict[str, Any],
    summary: str,
    surface: Any,
) -> dict[str, Any]:
    task_id = str(task.get("id") or "").strip()
    title = str(task.get("title") or "").strip() or task_id
    clean_summary = str(redact_secrets(summary or "Task dependencies are not satisfied."))
    started_at = now_iso()
    finished_at = now_iso()
    paths = record.paths
    ensure_execution_dirs(paths)
    report_path = paths.execution_reports_dir / f"{safe_task_file_component(task_id)}.md"
    report_path.write_text(
        "\n".join(
            [
                f"# Task Execution Report: {task_id}",
                "",
                f"- Task Title: {str(redact_secrets(title))}",
                f"- Started At: {started_at}",
                f"- Finished At: {finished_at}",
                "- Result: blocked",
                "- Result Kind: ide_review_execute",
                "- Patch: (none)",
                "- Verify Artifact: (none)",
                "- Merge Result: blocked before execution",
                "",
                "## Summary",
                "",
                clean_summary,
                "",
            ]
        ),
        encoding="utf-8",
    )
    set_task_status(plan, task_id, "blocked")
    save_plan(paths, plan)
    surface.emit_plan_node_updated(task_id, "blocked", title)
    surface.emit_swarm_worker_state_changed(task_id, "blocked", role="forge_execute_review")
    surface.emit_warning(clean_summary, worker_id=task_id, role="forge_execute_review")
    surface.emit_review_gate_decision(
        "blocked",
        clean_summary,
        worker_id=task_id,
        role="forge_execute_review",
    )
    return {
        "task_id": str(redact_secrets(task_id)),
        "status": "blocked",
        "success": False,
        "summary": clean_summary,
        "changed_files": [],
        "report_artifact_id": f"{forge_artifact_root_name(record.plan_id)}:{_repo_rel(paths.root, report_path)}",
        "patch_artifact_id": None,
        "context_artifact_id": None,
        "verify_summary": None,
    }


def _repo_rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _asset_surface(record: ForgePlanRecord, *, cfg: Any) -> Any:
    validate_forge_run_artifact_root(record)
    try:
        return build_asset_surface(cfg=cfg, run_paths=record.paths)
    except AssetError as e:
        raise ProtocolError("asset_error", str(redact_secrets(str(e)))) from e


def _asset_entries_payload(
    record: ForgePlanRecord,
    *,
    cfg: Any,
    include_deleted: bool,
) -> list[dict[str, Any]]:
    surface = _asset_surface(record, cfg=cfg)
    try:
        entries = surface.list_assets(include_deleted=include_deleted)
    except AssetError as e:
        raise ProtocolError("asset_error", str(redact_secrets(str(e)))) from e
    return [_asset_entry_payload(entry) for entry in entries]


def _asset_entry_payload(entry: AssetSurfaceEntry) -> dict[str, Any]:
    return redact_secrets(
        {
            "record": entry.record.to_dict(),
            "comprehension_status": entry.comprehension_status,
            "comprehension_source": entry.comprehension_source,
            "comprehension_summary_preview": entry.comprehension_summary_preview,
            "detected_language": entry.detected_language,
        }
    )


def _asset_detail_payload(detail: AssetSurfaceDetail) -> dict[str, Any]:
    return redact_secrets(
        {
            "record": detail.record.to_dict(),
            "comprehension_status": detail.comprehension_status,
            "comprehension": (
                detail.comprehension.to_dict() if detail.comprehension is not None else None
            ),
            "versions": detail.versions,
            "extracted_text_preview": detail.extracted_text_preview,
        }
    )


def _legacy_plan_assets_payload(plan: dict[str, Any]) -> list[dict[str, Any]]:
    assets = plan.get("assets")
    if not isinstance(assets, list):
        return []
    return [redact_secrets(dict(asset)) for asset in assets if isinstance(asset, dict)]


def _bind_asset_to_plan(record: ForgePlanRecord, *, surface: Any, asset_record: Any) -> list[str]:
    try:
        plan = load_plan(record.paths)
        active_records = surface.index.records(include_deleted=False)
        bound_task_ids = bind_asset_to_matching_tasks(
            plan=plan,
            record=asset_record,
            active_records=active_records,
        )
        if bound_task_ids:
            save_plan(record.paths, plan)
        return [str(task_id) for task_id in bound_task_ids]
    except (ForgeError, AssetError):
        return []


def _required_task_id(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ProtocolError("missing_field", "task_id is required.")
    if len(clean) > 128 or "/" in clean or "\\" in clean:
        raise ProtocolError("invalid_task_id", "task_id must be an opaque Forge task id.")
    return clean


def _plan_ide_revision(plan: dict[str, Any]) -> int:
    try:
        revision = int(plan.get("ide_revision", 0) or 0)
    except (TypeError, ValueError):
        revision = 0
    return max(0, revision)


def _check_expected_revision(plan: dict[str, Any], expected_revision: Any) -> int:
    current = _plan_ide_revision(plan)
    if expected_revision is None:
        return current
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError) as e:
        raise ProtocolError(
            "invalid_revision",
            "expected_revision must be an integer when provided.",
        ) from e
    if expected != current:
        raise ProtocolError(
            "stale_plan_revision",
            f"Plan revision is {current}; caller expected {expected}. Refresh plan state and retry.",
        )
    return current


def _safe_plan_edit_text(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if "\x00" in text:
        raise ProtocolError("invalid_field", f"{field} must not contain NUL bytes.")
    if len(text) > MAX_PLAN_EDIT_TEXT_CHARS:
        raise ProtocolError(
            "invalid_field",
            f"{field} is too large for an IDE plan edit.",
        )
    return str(redact_secrets(text))


def _regeneration_instruction(
    plan: dict[str, Any],
    *,
    instruction: str,
    focus: str,
) -> str:
    if instruction:
        base = instruction
    else:
        goal = str(redact_secrets(str(plan.get("project_goal") or ""))).strip()
        summary = str(redact_secrets(str(plan.get("summary") or ""))).strip()
        base = "Regenerate the active Forge plan using the current workspace context."
        if goal:
            base += f" Goal: {goal}."
        elif summary:
            base += f" Current summary: {summary}."
    if focus:
        return f"{base}\n\nFocus: {focus}"
    return base


def _plan_regeneration_fingerprint(plan: dict[str, Any]) -> str:
    comparable = dict(plan)
    for key in ("updated_at", "ide_revision", "ide_regenerated_at"):
        comparable.pop(key, None)
    return json.dumps(
        comparable,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _plan_assistant_payload(plan: dict[str, Any]) -> dict[str, Any]:
    raw = plan.get("ide_assistant")
    if not isinstance(raw, dict):
        return {"instruction": "", "updated_at": None, "source": "default"}
    return {
        "instruction": str(redact_secrets(str(raw.get("instruction") or ""))),
        "updated_at": str(raw.get("updated_at") or "") or None,
        "source": str(raw.get("source") or "ide_protocol"),
    }


def _required_asset_id(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ProtocolError("missing_field", "asset_id is required.")
    if ASSET_ID_PATTERN.fullmatch(clean) is None or "/" in clean or "\\" in clean:
        raise ProtocolError("invalid_asset_id", "asset_id must be an opaque Forge asset id.")
    return clean


def _resolve_workspace_asset_source(record: ForgePlanRecord, source_path: Path) -> Path:
    validate_forge_run_artifact_root(record)
    root = record.paths.root.expanduser().resolve()
    raw = Path(source_path).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise ProtocolError(
            "asset_path_outside_workspace",
            "Asset source paths must stay inside the resolved workspace.",
        ) from e
    if _path_or_parent_is_symlink(candidate, root=root):
        raise ProtocolError("asset_symlink_rejected", "Asset source paths may not be symlinks.")
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as e:
        raise ProtocolError(
            "asset_path_outside_workspace",
            "Asset source paths must stay inside the resolved workspace.",
        ) from e
    if not resolved.exists():
        raise ProtocolError("asset_not_found", "Asset source path does not exist.")
    if not resolved.is_file():
        raise ProtocolError("asset_not_file", "Asset source path must be a regular file.")
    try:
        size = resolved.stat().st_size
    except OSError as e:
        raise ProtocolError("asset_not_found", "Asset source path could not be inspected.") from e
    if size > MAX_FORGE_ASSET_BYTES:
        raise ProtocolError(
            "asset_too_large",
            f"Asset source exceeds the maximum supported size ({MAX_FORGE_ASSET_BYTES} bytes).",
        )
    return resolved


def _path_or_parent_is_symlink(path: Path, *, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    probe = root
    for part in relative.parts:
        probe = probe / part
        if _path_is_symlink(probe):
            return True
        if probe == path:
            break
    return False


def _read_json_artifact(path: Path, *, root: Path) -> dict[str, Any] | None:
    resolved = _resolve_artifact_file_for_read(path, root=root)
    if resolved is None:
        return None
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_text_artifact(path: Path, *, root: Path, max_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> str:
    resolved = _resolve_artifact_file_for_read(path, root=root)
    if resolved is None:
        return ""
    return _read_limited_text(resolved, max_bytes=max_bytes).text


def _resolve_artifact_file_for_read(path: Path, *, root: Path) -> Path | None:
    if _path_is_symlink(path):
        return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _plan_schema_version(plan: dict[str, Any]) -> int:
    try:
        return int(plan.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _legacy_asset_files(run_paths: RunPaths) -> list[Path]:
    files: list[Path] = []
    for root in (run_paths.assets_dir, run_paths.assets_text_dir):
        if _path_is_symlink(root):
            _raise_forge_registry_rejected()
        if not root.exists():
            continue
        root_resolved = _resolve_existing_directory(root, code="forge_registry_rejected")
        _ensure_resolved_under(root_resolved, run_paths.root.resolve())
        for path in sorted(root.rglob("*")):
            if _path_is_symlink(path):
                _raise_forge_registry_rejected()
            if path.is_file():
                files.append(path)
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_empty_legacy_dirs(run_paths: RunPaths) -> None:
    for root in (run_paths.assets_text_dir, run_paths.assets_dir):
        if not root.exists() or _path_is_symlink(root):
            continue
        for directory in sorted(
            [path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            with suppress(OSError):
                directory.rmdir()
        with suppress(OSError):
            root.rmdir()


def _verification_commands_for_task(task: dict[str, Any], fallback: list[str]) -> list[str]:
    explicit = _string_list(task.get("verification_commands") or task.get("verify_commands"))
    return explicit or list(fallback)


def _task_write_scope_for_execution(task: dict[str, Any]) -> list[str]:
    return _string_list(task.get("write_scope")) or _string_list(task.get("estimated_files"))


def _request_verify_approval(surface: Any, commands: list[str]) -> bool:
    decision = surface.request_approval(
        ApprovalRequest(
            kind="verify_run",
            reason="review mode requires confirmation for exact verification commands",
            preview="\n".join(f"$ {command}" for command in commands),
            command=_verification_command_label(commands),
            allow_for_session_scope=exact_verify_command_set_scope(commands),
        )
    )
    return bool(getattr(decision, "allow", False))


def _verification_command_label(commands: list[str]) -> str:
    return commands[0] if len(commands) == 1 else f"{len(commands)} verification commands"


def _task_attempt_count(task: dict[str, Any]) -> int:
    try:
        return max(1, int(task.get("attempts") or 0) + 1)
    except (TypeError, ValueError):
        return 1


def _optional_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _selection_dependency_blockers(
    plan: dict[str, Any],
    selected_tasks: list[dict[str, Any]],
) -> list[str]:
    completed = {
        str(task.get("id") or "").strip()
        for task in _plan_tasks(plan)
        if str(task.get("status") or "").strip().lower() in {"done", "completed"}
    }
    blockers: list[str] = []
    for task in selected_tasks:
        task_id = str(task.get("id") or "").strip() or "task"
        for dep_id in _string_list(task.get("dependencies")):
            if dep_id not in completed:
                dep_task = find_task(plan, dep_id)
                dep_status = (
                    str(dep_task.get("status") or "unknown").strip()
                    if isinstance(dep_task, dict)
                    else "missing"
                )
                blockers.append(f"{task_id}: dependency {dep_id} is not done ({dep_status}).")
        completed.add(task_id)
    return blockers


def _initial_completed_task_ids(plan: dict[str, Any]) -> set[str]:
    return {
        str(task.get("id") or "").strip()
        for task in _plan_tasks(plan)
        if str(task.get("id") or "").strip()
        and str(task.get("status") or "").strip().lower() in {"done", "completed"}
    }


def _runtime_dependency_blockers(
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    completed_task_ids: set[str],
) -> list[str]:
    task_id = str(task.get("id") or "").strip() or "task"
    blockers: list[str] = []
    for dep_id in _string_list(task.get("dependencies")):
        if dep_id in completed_task_ids:
            continue
        dep_task = find_task(plan, dep_id)
        dep_status = (
            str(dep_task.get("status") or "unknown").strip()
            if isinstance(dep_task, dict)
            else "missing"
        )
        blockers.append(
            f"{task_id}: dependency {dep_id} is not completed; current status is {dep_status}."
        )
    return blockers


def _plan_tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def _task_to_ide_schema(
    task: dict[str, Any],
    *,
    verification_commands: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    title = str(task.get("title") or "").strip() or task_id or "Untitled task"
    objective = str(task.get("description") or "").strip() or title
    explicit_verify = _string_list(task.get("verification_commands") or task.get("verify_commands"))
    return {
        "task_id": str(redact_secrets(task_id)),
        "title": str(redact_secrets(title)),
        "objective": str(redact_secrets(objective)),
        "file_scope": {
            "estimated_files": _redacted_string_list(task.get("estimated_files")),
            "write_scope": _redacted_string_list(task.get("write_scope")),
        },
        "acceptance_criteria": _redacted_string_list(task.get("acceptance_criteria")),
        "verification_commands": _redacted_strings(explicit_verify or verification_commands),
        "risk_notes": _redacted_string_list(
            task.get("risk_notes") or task.get("risks") or task.get("risk")
        ),
        "dependencies": _redacted_string_list(task.get("dependencies")),
        "order": _optional_int(task.get("order") or task.get("sequence")),
        "scope_unknown_reason": str(
            redact_secrets(str(task.get("scope_unknown_reason") or "").strip())
        ),
        "warnings": _redacted_strings(warnings),
        "status": str(redact_secrets(str(task.get("status") or "planned"))),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _redacted_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return _redacted_strings([value])
    return _redacted_strings(_string_list(value))


def _redacted_strings(values: list[str]) -> list[str]:
    return [str(redact_secrets(value)) for value in values if str(value).strip()]


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _workspace_context_payload(paths: RunPaths) -> dict[str, Any] | None:
    try:
        return ensure_workspace_context_artifacts(paths).to_dict()
    except Exception:
        return None


def _planner_knowledge_section(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    instruction: str,
) -> str | None:
    try:
        selected = prepare_planner_knowledge(
            paths=paths,
            plan=plan,
            user_text=instruction,
            selection_label="ide_forge_plan",
        )
        return selected.render_prompt_section(
            workspace_root=resolve_knowledge_workspace_root(paths)
        )
    except Exception:
        return None


def _resolved_verification_commands(cfg: Any, workspace_root: Path) -> list[str]:
    try:
        return _redacted_strings(
            resolve_verify_commands(cfg=cfg, verify_cmd=None, root=workspace_root)
        )
    except Exception:
        return []


def validate_ide_plan_quality(
    plan: dict[str, Any],
    *,
    verification_commands: list[str],
    extra_warnings: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    warnings = _sanitize_warnings([*list(extra_warnings), *validate_plan(plan)])
    task_warnings: dict[str, list[str]] = {}
    tasks = _plan_tasks(plan)
    if not tasks:
        warnings.append("Forge plan has no structured tasks.")
    for task in tasks:
        task_id = str(task.get("id") or "").strip() or "task"
        current: list[str] = []
        acceptance = _string_list(task.get("acceptance_criteria"))
        explicit_verify = _string_list(
            task.get("verification_commands") or task.get("verify_commands")
        )
        estimated = _string_list(task.get("estimated_files"))
        write_scope = _string_list(task.get("write_scope"))
        scope_unknown_reason = str(task.get("scope_unknown_reason") or "").strip()
        if _task_is_execution_ready(task):
            if not acceptance:
                current.append(f"{task_id}: missing acceptance criteria.")
            if not explicit_verify and not verification_commands:
                current.append(f"{task_id}: missing verification commands.")
            if not estimated and not write_scope and not scope_unknown_reason:
                current.append(
                    f"{task_id}: missing file scope and no scope_unknown_reason was recorded."
                )
        if current:
            task_warnings[task_id] = _sanitize_warnings(current)
            warnings.extend(task_warnings[task_id])
    sanitized = _sanitize_warnings(warnings)
    return {
        "warnings": sanitized,
        "task_warnings": task_warnings,
        "incomplete": bool(sanitized),
    }


def _task_is_execution_ready(task: dict[str, Any]) -> bool:
    if bool(task.get("analysis_only")):
        return False
    status = str(task.get("status") or "planned").strip().lower()
    return status not in {"blocked", "superseded", "invalidated", "done", "completed"}


def _select_preview_tasks(plan: dict[str, Any], task_ids: list[str]) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    by_id = {
        str(task.get("id") or "").strip(): task
        for task in tasks
        if str(task.get("id") or "").strip()
    }
    if task_ids:
        selected: list[dict[str, Any]] = []
        for task_id in task_ids:
            task = by_id.get(task_id)
            if task is None:
                raise ProtocolError("forge_task_not_found", f"Forge task was not found: {task_id}")
            selected.append(task)
        return selected
    return [task for task in tasks if _task_is_execution_ready(task)]


def _preview_file_scope(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(redact_secrets(str(task.get("id") or ""))),
        "title": str(redact_secrets(str(task.get("title") or ""))),
        "estimated_files": _redacted_string_list(task.get("estimated_files")),
        "write_scope": _redacted_string_list(task.get("write_scope")),
        "scope_unknown_reason": str(
            redact_secrets(str(task.get("scope_unknown_reason") or "").strip())
        ),
    }


def _preview_verification(task: dict[str, Any], fallback_commands: list[str]) -> dict[str, Any]:
    explicit = _string_list(task.get("verification_commands") or task.get("verify_commands"))
    commands = explicit or fallback_commands
    return {
        "task_id": str(redact_secrets(str(task.get("id") or ""))),
        "commands": _redacted_strings(commands),
        "source": "task" if explicit else ("plan_or_config" if commands else "missing"),
        "missing_reason": (
            ""
            if commands
            else "No task verification commands or plan-level verification commands are available."
        ),
    }


def _preview_known_risks(
    selected_tasks: list[dict[str, Any]],
    *,
    real_execution_supported: bool,
) -> list[str]:
    risks = [
        "Active Forge runtime cancellation is cooperative and may stop at the next backend checkpoint.",
    ]
    if not real_execution_supported:
        risks.insert(0, REAL_EXECUTE_AUTO_UNSUPPORTED_REASON)
    for task in selected_tasks:
        task_id = str(task.get("id") or "task").strip() or "task"
        for risk in _string_list(task.get("risk_notes") or task.get("risks") or task.get("risk")):
            risks.append(f"{task_id}: {risk}")
        for warning in _string_list(task.get("warnings")):
            risks.append(f"{task_id}: {warning}")
    return list(dict.fromkeys(risks))


def _preview_missing_prerequisites(
    *,
    selected_tasks: list[dict[str, Any]],
    verification: list[dict[str, Any]],
    workspace_trust_required: bool,
    workspace_trusted: bool | None,
    sandbox: dict[str, Any],
    sandbox_required: bool,
) -> list[str]:
    missing: list[str] = []
    if not selected_tasks:
        missing.append("No executable Forge tasks were selected.")
    if workspace_trust_required:
        if workspace_trusted is False:
            missing.append("Workspace Trust is required before mutating Forge execution can run.")
        elif workspace_trusted is None:
            missing.append(
                "Workspace Trust status is required before mutating Forge execution can run."
            )
    if sandbox.get("supported") is False:
        missing.append(str(sandbox.get("diagnostic") or "Sandbox profile is unsupported."))
    elif sandbox_required and sandbox.get("available") is not True:
        missing.append(str(sandbox.get("diagnostic") or "Sandbox profile is not available."))
    verification_by_id = {
        str(item.get("task_id") or ""): item for item in verification if isinstance(item, dict)
    }
    for task in selected_tasks:
        task_id = str(task.get("id") or "task").strip() or "task"
        if not _task_is_execution_ready(task):
            status = str(task.get("status") or "unknown").strip().lower() or "unknown"
            missing.append(f"{task_id}: task status '{status}' is not executable.")
            continue
        if not _string_list(task.get("acceptance_criteria")):
            missing.append(f"{task_id}: acceptance criteria are required before execution.")
        verify_item = verification_by_id.get(task_id)
        if not verify_item or not _string_list(verify_item.get("commands")):
            missing.append(f"{task_id}: verification commands are required before execution.")
        if _task_is_execution_ready(task):
            estimated = _string_list(task.get("estimated_files"))
            write_scope = _string_list(task.get("write_scope"))
            scope_unknown_reason = str(task.get("scope_unknown_reason") or "").strip()
            if not estimated and not write_scope and not scope_unknown_reason:
                missing.append(
                    f"{task_id}: file scope is required, or scope_unknown_reason must explain why it is unknown."
                )
    return list(dict.fromkeys(missing))


def _preview_required_approvals(
    *,
    mode: str,
    selected_tasks: list[dict[str, Any]],
    verification: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if mode == "readonly":
        return []
    approvals: list[dict[str, Any]] = []
    executable_task_ids = {
        str(task.get("id") or "").strip()
        for task in selected_tasks
        if _task_is_execution_ready(task)
    }
    for task in selected_tasks:
        if not _task_is_execution_ready(task):
            continue
        task_id = str(task.get("id") or "").strip()
        files = _string_list(task.get("write_scope")) or _string_list(task.get("estimated_files"))
        if files:
            scope = exact_file_set_scope(files, operation="fs_write")
            approvals.append(
                {
                    "kind": "fs_write",
                    "task_id": str(redact_secrets(task_id)),
                    "reason": "Forge execution may write the scoped task files.",
                    "scope": scope,
                    "allow_for_session_scope": scope,
                    "allow_for_session_supported": True,
                }
            )
    for item in verification:
        task_id = str(item.get("task_id") or "").strip() if isinstance(item, dict) else ""
        if task_id not in executable_task_ids:
            continue
        commands = _string_list(item.get("commands")) if isinstance(item, dict) else []
        if commands:
            scope = exact_verify_command_set_scope(commands)
            approvals.append(
                {
                    "kind": "verify_run",
                    "task_id": str(redact_secrets(task_id)),
                    "reason": "Forge execution may run the exact verification command set.",
                    "scope": scope,
                    "allow_for_session_scope": scope,
                    "allow_for_session_supported": True,
                }
            )
    return redact_secrets(approvals)


def _preview_runtime_approval_requirements(mode: str) -> list[dict[str, Any]]:
    if mode == "readonly":
        return []
    return redact_secrets(
        [
            {
                "kind": "shell_run",
                "reason": (
                    "If review execution runs shell commands, each command is evaluated before "
                    "execution and requires a host-managed exact command-hash approval."
                ),
                "scope_requirement": {
                    "type": "exact_command_hash",
                    "deferred_until_command_known": True,
                },
                "allow_for_session_supported": True,
                "warning": None,
            },
            {
                "kind": "custom_tool_run",
                "reason": (
                    "If review execution invokes a custom tool, the tool invocation is host-managed. "
                    "Unscoped custom-tool approvals cannot be approved broadly for the session."
                ),
                "scope_requirement": None,
                "allow_for_session_supported": False,
                "warning": "Allow for session is disabled unless the runtime provides an explicit safe scope.",
            },
            {
                "kind": "mcp_tool_run",
                "reason": (
                    "If review execution invokes MCP-like tools, the invocation remains host-managed "
                    "and must not receive broad session approval by tool kind."
                ),
                "scope_requirement": None,
                "allow_for_session_supported": False,
                "warning": "Allow for session is disabled unless the runtime provides an explicit safe scope.",
            },
        ]
    )


def _approval_scopes_are_safe(approvals: list[dict[str, Any]]) -> bool:
    allowed = {"exact_file_set", "exact_verify_command_set"}
    for approval in approvals:
        scope = approval.get("allow_for_session_scope") or approval.get("scope")
        if not isinstance(scope, dict) or str(scope.get("type") or "") not in allowed:
            return False
    return True


def _sandbox_preview(raw_profile: str, *, cfg: Any | None = None) -> dict[str, Any]:
    profile = raw_profile.strip().lower() or "default"
    supported = profile in SUPPORTED_SANDBOX_PROFILES
    if not supported:
        diagnostic = f"Unsupported sandbox profile for IDE preview: {profile}"
        return {
            "requested": str(redact_secrets(profile)),
            "supported": False,
            "available": False,
            "diagnostic": diagnostic,
        }
    if profile == "off":
        diagnostic = "Sandbox profile 'off' disables sandboxing; mutating Forge execution requires an available sandbox profile."
        return {
            "requested": str(redact_secrets(profile)),
            "supported": True,
            "available": False,
            "diagnostic": diagnostic,
        }
    if profile == "warn":
        diagnostic = (
            "Sandbox profile 'warn' can fall back to host execution; mutating IDE Forge Execute "
            "requires strict sandboxing."
        )
        return {
            "requested": str(redact_secrets(profile)),
            "supported": True,
            "available": False,
            "diagnostic": diagnostic,
        }
    if cfg is None:
        diagnostic = (
            "Sandbox availability is not verified by this bridge call. Run the sandbox doctor "
            "or start the IDE bridge with a model-backed execution request before mutating execution."
        )
        return {
            "requested": str(redact_secrets(profile)),
            "supported": True,
            "available": False,
            "diagnostic": diagnostic,
        }

    sandbox_cfg = _cfg_for_sandbox_profile(cfg, profile)
    try:
        diagnostic_result = diagnose_sandbox(
            sandbox_cfg,
            include_smoke=False,
            include_server_image=False,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics are environment-dependent
        return {
            "requested": str(redact_secrets(profile)),
            "supported": True,
            "available": False,
            "diagnostic": str(redact_secrets(f"Sandbox diagnostic failed: {exc}")),
        }

    configured_mode = str(getattr(diagnostic_result, "configured_mode", "") or "").strip().lower()
    available = bool(getattr(diagnostic_result, "ready", False)) and configured_mode == "strict"
    if available:
        diagnostic = (
            f"Sandbox ready using {diagnostic_result.selected_backend or configured_mode} "
            f"({configured_mode})."
        )
    elif configured_mode != "strict":
        diagnostic = (
            f"Sandbox mode is {configured_mode or 'unknown'}; mutating IDE Forge Execute "
            "requires strict sandboxing."
        )
    else:
        next_steps = "; ".join(
            str(item) for item in getattr(diagnostic_result, "next_steps", ()) if str(item)
        )
        status = str(getattr(diagnostic_result, "status", "") or "unavailable")
        diagnostic = next_steps or f"Sandbox is not available ({status})."
    return {
        "requested": str(redact_secrets(profile)),
        "supported": supported,
        "available": available,
        "diagnostic": str(redact_secrets(diagnostic)),
    }


def _cfg_for_sandbox_profile(cfg: Any, profile: str) -> Any:
    if profile == "default":
        return cfg
    try:
        effective = cfg.model_copy(deep=True)
    except AttributeError:
        effective = cfg
    extra_fields = dict(getattr(effective, "extra_fields", {}) or {})
    shell_sandbox = dict(extra_fields.get("shell_sandbox") or {})
    shell_sandbox["mode"] = profile
    extra_fields["shell_sandbox"] = shell_sandbox
    verify_sandbox = dict(extra_fields.get("verify_sandbox") or {})
    verify_sandbox["mode"] = profile
    extra_fields["verify_sandbox"] = verify_sandbox
    try:
        effective.extra_fields = extra_fields
    except Exception:
        pass
    return effective


def _preview_next_action(
    *,
    preview_ready: bool,
    missing_prerequisites: list[str],
    real_execution_supported: bool,
) -> str:
    if missing_prerequisites:
        return "Resolve the missing prerequisites, then run Forge Execute Preview again."
    if not real_execution_supported:
        return "Review the preview result. Real execution is unavailable for the requested mode."
    if preview_ready:
        return "Review the preview, then explicitly confirm real review-mode execution."
    return "Review the preview result before attempting execution."


def _sanitize_warnings(values: list[str] | tuple[str, ...]) -> list[str]:
    return list(
        dict.fromkeys(str(redact_secrets(value)).strip() for value in values if str(value).strip())
    )


def _write_plan_validation_artifact(*, paths: RunPaths, warnings: list[str]) -> None:
    lines = ["# Plan Validation", ""]
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("No IDE plan quality warnings.")
    try:
        paths.notes_dir.mkdir(parents=True, exist_ok=True)
        (paths.notes_dir / "plan_validation.md").write_text(
            "\n".join(lines).rstrip() + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def _bounded_plan_count(value: Any) -> int:
    if value is None or value == "":
        return MAX_LISTED_PLANS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return MAX_LISTED_PLANS
    return max(1, min(parsed, MAX_LISTED_PLANS))


def _iter_persisted_plans(
    workspace_root: Path,
    *,
    limit: int,
) -> list[tuple[RunPaths, dict[str, Any]]]:
    registry = _validated_registry_base(workspace_root, missing_ok=True)
    if registry is None:
        return []
    root, _runtime_dir, runs_dir = registry
    out: list[tuple[RunPaths, dict[str, Any]]] = []
    for run_dir in sorted(runs_dir.iterdir(), key=lambda path: path.name, reverse=True):
        if len(out) >= limit:
            break
        if run_dir.is_symlink() or not run_dir.is_dir() or not _valid_plan_id(run_dir.name):
            continue
        try:
            paths = _paths_for_persisted_plan(workspace_root=root, plan_id=run_dir.name)
            plan = load_plan(paths)
        except (ForgeError, ProtocolError, OSError):
            continue
        out.append((paths, plan))
    return out


def _paths_for_persisted_plan(*, workspace_root: Path, plan_id: str) -> RunPaths:
    clean = plan_id.strip()
    if not _valid_plan_id(clean):
        raise ProtocolError("invalid_plan_id", "plan_id must be an opaque Forge plan id.")
    registry = _validated_registry_base(workspace_root, missing_ok=False)
    if registry is None:
        raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
    root, _runtime_dir, runs_dir = registry
    paths = make_run_paths(root=root, run_id=clean)
    try:
        paths.run_dir.resolve(strict=False).relative_to(runs_dir)
    except (OSError, ValueError) as e:
        raise ProtocolError("invalid_plan_id", "plan_id escapes the Forge run registry.") from e
    if not paths.run_dir.exists():
        raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
    _validate_forge_run_paths(paths, require_plan_json=True)
    return paths


def _validated_registry_base(
    workspace_root: Path,
    *,
    missing_ok: bool,
) -> tuple[Path, Path, Path] | None:
    root = workspace_root.expanduser().resolve()
    runtime_dir = root / ".alysis"
    if _path_is_symlink(runtime_dir):
        _raise_forge_registry_rejected()
    if not runtime_dir.exists():
        if missing_ok:
            return None
        raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
    runtime_resolved = _resolve_existing_directory(runtime_dir, code="forge_registry_rejected")
    _ensure_resolved_under(runtime_resolved, root)
    runs_dir = runtime_dir / "runs"
    if _path_is_symlink(runs_dir):
        _raise_forge_registry_rejected()
    if not runs_dir.exists():
        if missing_ok:
            return None
        raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
    runs_resolved = _resolve_existing_directory(runs_dir, code="forge_registry_rejected")
    _ensure_resolved_under(runs_resolved, root)
    _ensure_resolved_under(runs_resolved, runtime_resolved)
    return root, runtime_resolved, runs_resolved


def _validate_forge_run_paths(paths: RunPaths, *, require_plan_json: bool) -> None:
    registry = _validated_registry_base(paths.root, missing_ok=False)
    if registry is None:
        raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
    root, _runtime_dir, runs_dir = registry
    run_dir = paths.run_dir
    if _path_is_symlink(run_dir):
        _raise_forge_registry_rejected()
    if not run_dir.exists():
        raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
    run_resolved = _resolve_existing_directory(run_dir, code="forge_registry_rejected")
    _ensure_resolved_under(run_resolved, root)
    _ensure_resolved_under(run_resolved, runs_dir)

    plan_dir = paths.plan_dir
    if _path_is_symlink(plan_dir):
        _raise_forge_registry_rejected()
    if require_plan_json and not plan_dir.exists():
        raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
    if plan_dir.exists():
        plan_resolved = _resolve_existing_directory(plan_dir, code="forge_registry_rejected")
        _ensure_resolved_under(plan_resolved, root)
        _ensure_resolved_under(plan_resolved, run_resolved)

    if require_plan_json:
        _resolve_existing_file(
            paths.plan_json_path,
            workspace_root=root,
            run_root=run_resolved,
            not_found_code="forge_plan_not_found",
        )

    _validate_optional_directory(paths.execution_dir, workspace_root=root, run_root=run_resolved)
    _validate_optional_directory(
        paths.execution_patches_dir,
        workspace_root=root,
        run_root=run_resolved,
    )


def _validated_forge_patches_dir(record: ForgePlanRecord) -> Path | None:
    _validate_forge_run_paths(record.paths, require_plan_json=True)
    execution_dir = record.paths.execution_dir
    if not execution_dir.exists():
        return None
    execution_resolved = _resolve_existing_directory(
        execution_dir,
        code="forge_registry_rejected",
    )
    patches_dir = record.paths.execution_patches_dir
    if _path_is_symlink(patches_dir):
        _raise_forge_registry_rejected()
    if not patches_dir.exists():
        return None
    patches_resolved = _resolve_existing_directory(patches_dir, code="forge_registry_rejected")
    _ensure_resolved_under(patches_resolved, execution_resolved)
    return patches_resolved


def _validate_optional_directory(path: Path, *, workspace_root: Path, run_root: Path) -> None:
    if _path_is_symlink(path):
        _raise_forge_registry_rejected()
    if not path.exists():
        return
    resolved = _resolve_existing_directory(path, code="forge_registry_rejected")
    _ensure_resolved_under(resolved, workspace_root)
    _ensure_resolved_under(resolved, run_root)


def _resolve_existing_directory(path: Path, *, code: str) -> Path:
    try:
        if not path.is_dir():
            if code == "forge_plan_not_found":
                raise ProtocolError("forge_plan_not_found", "Forge plan was not found.")
            _raise_forge_registry_rejected()
        return path.resolve(strict=True)
    except OSError as e:
        if code == "forge_plan_not_found":
            raise ProtocolError("forge_plan_not_found", "Forge plan was not found.") from e
        _raise_forge_registry_rejected(e)


def _resolve_existing_file(
    path: Path,
    *,
    workspace_root: Path,
    run_root: Path,
    not_found_code: str,
) -> Path:
    if _path_is_symlink(path):
        _raise_forge_registry_rejected()
    try:
        if not path.is_file():
            raise ProtocolError(not_found_code, "Forge plan was not found.")
        resolved = path.resolve(strict=True)
    except OSError as e:
        raise ProtocolError(not_found_code, "Forge plan was not found.") from e
    try:
        resolved.relative_to(workspace_root)
        resolved.relative_to(run_root)
    except ValueError as e:
        _raise_forge_registry_rejected(e)
    return resolved


def _ensure_resolved_under(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as e:
        _raise_forge_registry_rejected(e)


def _path_is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        _raise_forge_registry_rejected()


def _raise_forge_registry_rejected(exc: BaseException | None = None) -> None:
    error = ProtocolError("forge_registry_rejected", FORGE_REGISTRY_REJECTED_MESSAGE)
    if exc is None:
        raise error
    raise error from exc


def _valid_plan_id(value: str) -> bool:
    return bool(PLAN_ID_PATTERN.fullmatch(value)) and "/" not in value and "\\" not in value


def _plan_summary(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    session_id: str | None,
    source: str,
) -> dict[str, Any]:
    tasks = _plan_tasks(plan)
    root_name = forge_artifact_root_name(paths.run_id)
    return {
        "plan_id": paths.run_id,
        "session_id": session_id,
        "workspace_root": str(paths.root),
        "status": _plan_status(tasks),
        "source": source,
        "project_goal": str(redact_secrets(str(plan.get("project_goal") or ""))),
        "summary": str(redact_secrets(str(plan.get("summary") or ""))),
        "task_count": len(tasks),
        "created_at": str(plan.get("created_at") or ""),
        "updated_at": str(plan.get("updated_at") or ""),
        "plan_artifact_id": f"{root_name}:plan/plan.json",
        "plan_markdown_artifact_id": f"{root_name}:plan/PLAN.md",
    }


def _plan_status(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "incomplete"
    statuses = {str(task.get("status") or "planned").strip().lower() for task in tasks}
    if statuses <= {"done", "completed"}:
        return "completed"
    if "blocked" in statuses:
        return "blocked"
    return "planned"


def _diff_id(record: ForgePlanRecord, rel_path: str) -> str:
    digest = hashlib.sha256(
        f"{record.session_id}\0{record.plan_id}\0{rel_path}".encode("utf-8", errors="replace")
    ).hexdigest()
    return f"diff_{digest[:32]}"


def _diff_file_label(changed_files: list[str]) -> str:
    if not changed_files:
        return "(unknown)"
    if len(changed_files) == 1:
        return _safe_rel_label(changed_files[0])
    return f"{_safe_rel_label(changed_files[0])} (+{len(changed_files) - 1} more)"


def _safe_rel_label(path: str) -> str:
    pure = PurePosixPath(path.replace("\\", "/"))
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return Path(path).name or "(unknown)"
    return pure.as_posix()


@dataclass(frozen=True, slots=True)
class _LimitedText:
    text: str
    truncated: bool


def _read_limited_text(path: Path, *, max_bytes: int) -> _LimitedText:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except OSError as e:
        raise ProtocolError("diff_not_readable", "Diff artifact was not readable.") from e
    truncated = len(payload) > max_bytes or size > max_bytes
    return _LimitedText(
        text=payload[:max_bytes].decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def _bounded_diff_bytes(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_MAX_DIFF_BYTES
    try:
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise ProtocolError("invalid_field", "max_bytes must be an integer.") from e
    return max(1, min(parsed, MAX_DIFF_BYTES))
