from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..approval_scope import ApprovalSessionScope, approval_session_scope_for_request
from ..surface.events import Event, PromptForInput
from ..surface.types import ApprovalDecision, ApprovalRequest
from .protocol import ProtocolError, redact_secrets

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120.0
APPROVAL_ID_MAX_LENGTH = 128
APPROVAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
DEFAULT_RESOLVED_RETENTION_SECONDS = 300.0
DEFAULT_MAX_RESOLVED_APPROVALS = 512


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _expires_at(timeout_seconds: float) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=max(0.0, timeout_seconds)))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    kind: str
    reason: str
    preview: str
    created_at: str
    expires_at: str
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    decision: ApprovalDecision | None = None
    state: str = "pending"
    allow_for_session_supported: bool = False
    allow_for_session_scope: dict[str, Any] | None = None
    allow_for_session_key: str | None = None
    allow_for_session_warning: str | None = None
    resolved_at: float | None = None


class ApprovalBroker:
    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        resolved_retention_seconds: float = DEFAULT_RESOLVED_RETENTION_SECONDS,
        max_resolved_approvals: int = DEFAULT_MAX_RESOLVED_APPROVALS,
    ) -> None:
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.resolved_retention_seconds = max(0.0, float(resolved_retention_seconds))
        self.max_resolved_approvals = max(0, int(max_resolved_approvals))
        self._lock = threading.RLock()
        self._approvals: dict[str, PendingApproval] = {}
        self._session_allowances: set[tuple[str, str]] = set()

    def request(
        self,
        *,
        session_id: str,
        request: ApprovalRequest,
        emit_event: Callable[[Event], None],
    ) -> ApprovalDecision:
        kind = str(redact_secrets(request.kind or "approval"))
        scope_info = approval_session_scope_for_request(request)
        allowance_key = _allowance_key(session_id, scope_info)
        with self._lock:
            self._prune_locked(time.monotonic())
            if allowance_key is not None and allowance_key in self._session_allowances:
                return ApprovalDecision(allow=True, allow_for_session=True)

        approval_id = _make_approval_id()
        preview = str(redact_secrets(request.preview or ""))
        reason = str(redact_secrets(request.reason or ""))
        pending = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            kind=kind,
            reason=reason,
            preview=preview,
            created_at=_now_iso(),
            expires_at=_expires_at(self.timeout_seconds),
            deadline=time.monotonic() + self.timeout_seconds,
            allow_for_session_supported=scope_info.supported,
            allow_for_session_scope=scope_info.scope,
            allow_for_session_key=scope_info.key,
            allow_for_session_warning=scope_info.warning,
        )
        with self._lock:
            self._approvals[approval_id] = pending

        emit_event(_approval_event(request, pending))

        if not pending.done.wait(timeout=self.timeout_seconds):
            expired_event: Event | None = None
            with self._lock:
                if pending.state == "pending":
                    pending.state = "expired"
                    pending.decision = ApprovalDecision(allow=False)
                    pending.resolved_at = time.monotonic()
                    expired_event = _approval_result_event(pending, status="expired")
                    self._prune_locked(pending.resolved_at)
            if expired_event is not None:
                emit_event(expired_event)
            return ApprovalDecision(allow=False)

        with self._lock:
            decision = pending.decision or ApprovalDecision(allow=False)
            if (
                decision.allow
                and decision.allow_for_session
                and pending.allow_for_session_key is not None
            ):
                self._session_allowances.add((session_id, pending.allow_for_session_key))
            if pending.resolved_at is None:
                pending.resolved_at = time.monotonic()
            result_status = "expired" if pending.state == "expired" else "resolved"
            self._prune_locked(pending.resolved_at)
        emit_event(_approval_result_event(pending, status=result_status, decision=decision))
        return decision

    def resolve(
        self,
        *,
        session_id: str,
        approval_id: str,
        allow: bool,
        allow_for_session: bool,
    ) -> dict[str, Any]:
        _validate_approval_id(approval_id)
        with self._lock:
            pending = self._approvals.get(approval_id)
            if pending is None:
                raise ProtocolError("approval_not_found", "Approval id is not pending.")
            if pending.session_id != session_id:
                raise ProtocolError(
                    "approval_not_found", "Approval id is not pending for this session."
                )
            if pending.state == "resolved":
                raise ProtocolError(
                    "approval_already_resolved", "Approval id was already resolved."
                )
            if pending.state == "expired" or time.monotonic() > pending.deadline:
                pending.state = "expired"
                pending.decision = ApprovalDecision(allow=False)
                pending.resolved_at = time.monotonic()
                pending.done.set()
                raise ProtocolError("approval_expired", "Approval id has expired.")
            requested_allow_for_session = bool(allow_for_session)
            effective_allow_for_session = (
                bool(allow)
                and requested_allow_for_session
                and pending.allow_for_session_supported
                and pending.allow_for_session_key is not None
            )
            warning = pending.allow_for_session_warning
            if bool(allow) and requested_allow_for_session and not effective_allow_for_session:
                warning = warning or (
                    "Allow for session was downgraded to allow once because this approval "
                    "does not have a supported exact scope."
                )
                pending.allow_for_session_warning = warning
            pending.state = "resolved"
            pending.decision = ApprovalDecision(
                allow=bool(allow),
                allow_for_session=effective_allow_for_session,
            )
            pending.resolved_at = time.monotonic()
            if pending.decision.allow and pending.decision.allow_for_session:
                self._session_allowances.add((session_id, pending.allow_for_session_key or ""))
            pending.done.set()
            self._prune_locked(pending.resolved_at)
            return {
                "session_id": session_id,
                "approval_id": approval_id,
                "status": "resolved",
                "allow": bool(allow),
                "allow_for_session": effective_allow_for_session,
                "allow_for_session_supported": pending.allow_for_session_supported,
                "allow_for_session_scope": _json_safe_value(
                    redact_secrets(pending.allow_for_session_scope)
                ),
                "allow_for_session_warning": str(redact_secrets(warning)) if warning else None,
            }

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._session_allowances = {
                key for key in self._session_allowances if key[0] != session_id
            }
            for approval_id, pending in list(self._approvals.items()):
                if pending.session_id != session_id:
                    continue
                if pending.state == "pending":
                    pending.state = "expired"
                    pending.decision = ApprovalDecision(allow=False)
                    pending.resolved_at = time.monotonic()
                    pending.done.set()
                del self._approvals[approval_id]

    def _prune_locked(self, now: float) -> None:
        if self.resolved_retention_seconds > 0:
            cutoff = now - self.resolved_retention_seconds
            for approval_id, pending in list(self._approvals.items()):
                if pending.state == "pending":
                    continue
                if pending.resolved_at is not None and pending.resolved_at < cutoff:
                    del self._approvals[approval_id]

        resolved = [
            (pending.resolved_at or now, approval_id)
            for approval_id, pending in self._approvals.items()
            if pending.state != "pending"
        ]
        overflow = len(resolved) - self.max_resolved_approvals
        if overflow <= 0:
            return
        for _, approval_id in sorted(resolved)[:overflow]:
            self._approvals.pop(approval_id, None)


def _approval_event(request: ApprovalRequest, pending: PendingApproval) -> Event:
    preview = pending.preview
    reason = pending.reason
    prompt_text = preview or reason or f"Approval required: {pending.kind}"
    command = request.command
    if command is not None:
        command = str(redact_secrets(command))
    files = [str(redact_secrets(path)) for path in request.files]
    metadata = _json_safe(redact_secrets(request.metadata))
    metadata.setdefault("approval_kind", pending.kind)
    allow_for_session_scope = _json_safe_value(redact_secrets(pending.allow_for_session_scope))
    allow_for_session_warning = (
        str(redact_secrets(pending.allow_for_session_warning))
        if pending.allow_for_session_warning
        else None
    )
    return PromptForInput(
        prompt_id=pending.approval_id,
        prompt_text=prompt_text,
        kind="approval",
        approval_kind=pending.kind,
        approval_id=pending.approval_id,
        reason=reason,
        preview=preview,
        files=files,
        command=command,
        metadata=metadata,
        expires_at=pending.expires_at,
        allow_for_session_supported=pending.allow_for_session_supported,
        allow_for_session_scope=allow_for_session_scope,
        allow_for_session_warning=allow_for_session_warning,
    )


def _approval_result_event(
    pending: PendingApproval,
    *,
    status: str,
    decision: ApprovalDecision | None = None,
) -> Event:
    decision = decision or pending.decision or ApprovalDecision(allow=False)
    return PromptForInput(
        prompt_id=pending.approval_id,
        prompt_text=pending.preview or pending.reason or f"Approval {status}: {pending.kind}",
        kind="approval_result",
        approval_id=pending.approval_id,
        reason=pending.reason,
        preview=pending.preview,
        metadata={
            "status": status,
            "kind": pending.kind,
            "approval_kind": pending.kind,
            "allow": bool(decision.allow),
            "allow_for_session": bool(decision.allow_for_session),
            "allow_for_session_supported": pending.allow_for_session_supported,
            "allow_for_session_scope": _json_safe_value(
                redact_secrets(pending.allow_for_session_scope)
            ),
            "allow_for_session_warning": str(redact_secrets(pending.allow_for_session_warning))
            if pending.allow_for_session_warning
            else None,
        },
        approval_kind=pending.kind,
        expires_at=pending.expires_at,
        allow_for_session_supported=pending.allow_for_session_supported,
        allow_for_session_scope=_json_safe_value(redact_secrets(pending.allow_for_session_scope)),
        allow_for_session_warning=str(redact_secrets(pending.allow_for_session_warning))
        if pending.allow_for_session_warning
        else None,
    )


def _json_safe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def _json_safe_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def _allowance_key(
    session_id: str,
    scope_info: ApprovalSessionScope,
) -> tuple[str, str] | None:
    if not scope_info.supported or scope_info.key is None:
        return None
    return (session_id, scope_info.key)


def _make_approval_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"approval_{ts}_{uuid.uuid4().hex[:12]}"


def _validate_approval_id(approval_id: str) -> None:
    if not approval_id or len(approval_id) > APPROVAL_ID_MAX_LENGTH:
        raise ProtocolError("invalid_approval_id", "approval_id length is invalid.")
    if APPROVAL_ID_PATTERN.fullmatch(approval_id) is None:
        raise ProtocolError("invalid_approval_id", "approval_id contains unsupported characters.")
