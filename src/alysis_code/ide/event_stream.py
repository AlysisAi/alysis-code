from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..surface.events import (
    ConfigFormRequest,
    ErrorRaised,
    Event,
    InfoEmitted,
    MessageDelta,
    MessageEnd,
    ModeChanged,
    PersonaChanged,
    PlanNodeUpdated,
    PromptForInput,
    ReviewGateDecision,
    StatusUpdate,
    SubagentStateChanged,
    SwarmWorkerStateChanged,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
    VerifyGateResult,
    WarningEmitted,
)
from ..surface.noop_surface import NoopSurface
from ..surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)
from .activity_events import (
    ACTIVITY_EVENT_TYPE,
    ActivityEvent,
    patch_activity,
    sanitize_activity_metadata,
    semantic_tool_name,
    tool_activity,
)
from .protocol import PROTOCOL_VERSION, redact_secrets

ApprovalEmitter = Callable[[Any], None]
ApprovalHandler = Callable[[ApprovalRequest, ApprovalEmitter], ApprovalDecision]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_lifecycle_text(value: object, *, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= maximum else f"{text[: maximum - 3].rstrip()}..."


@dataclass(frozen=True, slots=True)
class EventContext:
    session_id: str
    run_id: str | None = None
    job_id: str | None = None


class EventSequencer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0

    def envelope(
        self,
        event: Event,
        *,
        context: EventContext,
    ) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        data = event.to_dict()
        event_type = str(data.pop("type"))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "job_id": context.job_id,
            "sequence": sequence,
            "timestamp": _now_iso(),
            "type": event_type,
            "payload": redact_secrets(data),
        }


@dataclass(frozen=True, slots=True)
class ProtocolPayloadEvent:
    event_type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.event_type, **self.payload}


class ProtocolEventSurface(NoopSurface):
    host_managed_approvals = True
    canonical_message_tool_events = True

    def __init__(
        self,
        *,
        context: EventContext,
        emit: Callable[[dict[str, Any]], None],
        sequencer: EventSequencer | None = None,
        approval_handler: ApprovalHandler | None = None,
        semantic_activity_events: bool = False,
    ) -> None:
        self._base_context = context
        self._emit_message = emit
        self._sequencer = sequencer or EventSequencer()
        self._approval_handler = approval_handler
        self._semantic_activity_events = semantic_activity_events
        self._thread_context = threading.local()
        self._trace_level = "compact"

    @property
    def context(self) -> EventContext:
        job_id = getattr(self._thread_context, "job_id", None)
        run_id = getattr(self._thread_context, "run_id", None)
        return EventContext(
            session_id=self._base_context.session_id,
            run_id=run_id if run_id is not None else self._base_context.run_id,
            job_id=job_id if job_id is not None else self._base_context.job_id,
        )

    def with_job(self, job_id: str | None) -> ProtocolEventSurface:
        self._thread_context.job_id = job_id
        return self

    @property
    def trace_level(self) -> str:
        return self._trace_level

    def set_trace_level(self, level: str) -> str:
        normalized = str(level or "").strip().lower()
        if normalized not in {"off", "compact", "full"}:
            normalized = "compact"
        self._trace_level = normalized
        return self._trace_level

    def _emit_event(self, event: Event) -> None:
        self._emit_message(self._sequencer.envelope(event, context=self.context))

    def emit(self, event: Event) -> None:
        self._emit_event(event)

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(MessageDelta(text=text, worker_id=worker_id, role=role))

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(MessageEnd(text=text, worker_id=worker_id, role=role))

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            ToolCallStarted(
                call_id=call_id,
                name=semantic_tool_name(name),
                arguments_preview=arguments_preview,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_activity(self, event: ActivityEvent) -> None:
        self._emit_event(ProtocolPayloadEvent(ACTIVITY_EVENT_TYPE, event.to_payload()))

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            ToolCallProgress(call_id=call_id, text=text, worker_id=worker_id, role=role)
        )

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            ToolCallCompleted(
                call_id=call_id,
                success=success,
                result_preview=result_preview,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_status_update(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cached_tokens: int | None = None,
        cost_usd: float | None = None,
        mode: str | None = None,
        model: str | None = None,
        step: int | None = None,
        step_budget: int | None = None,
    ) -> None:
        self._emit_event(
            StatusUpdate(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_tokens=cached_tokens,
                cost_usd=cost_usd,
                mode=mode,
                model=model,
                step=step,
                step_budget=step_budget,
            )
        )

    def emit_mode_changed(self, mode: str) -> None:
        self._emit_event(ModeChanged(mode=mode))

    def emit_persona_changed(self, persona: str, effective_mode: str, source: str = "user") -> None:
        self._emit_event(
            PersonaChanged(persona=persona, effective_mode=effective_mode, source=source)
        )

    def emit_plan_node_updated(
        self,
        node_id: str,
        state: str,
        summary: str | None = None,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            PlanNodeUpdated(
                node_id=node_id,
                state=state,
                summary=summary,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_swarm_worker_state_changed(
        self,
        worker_id: str,
        state: str,
        *,
        role: str | None = None,
    ) -> None:
        self._emit_event(SwarmWorkerStateChanged(worker_id=worker_id, state=state, role=role))

    def on_subagent_start(self, event: SubagentStartEvent) -> None:
        run_id = str(event.subagent_run_id or "").strip() or uuid.uuid4().hex
        self._thread_context.subagent_run_id = run_id
        self._emit_event(
            SubagentStateChanged(
                subagent_run_id=run_id,
                name=event.name,
                mode=event.mode,
                state="running",
                label=event.label,
                subagent_session_id=event.subagent_session_id,
                description=_bounded_lifecycle_text(event.description, maximum=240),
            )
        )

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        run_id = (
            str(event.subagent_run_id or "").strip()
            or str(getattr(self._thread_context, "subagent_run_id", "") or "").strip()
            or uuid.uuid4().hex
        )
        self._emit_event(
            SubagentStateChanged(
                subagent_run_id=run_id,
                name=event.name,
                mode=event.mode,
                state=event.status,
                label=event.label,
                subagent_session_id=event.subagent_session_id,
                elapsed_ms=event.elapsed_ms,
                steps_completed=event.steps_completed,
                error=(
                    _bounded_lifecycle_text(event.error, maximum=600)
                    if event.error is not None
                    else None
                ),
            )
        )
        self._thread_context.subagent_run_id = None

    def emit_verify_gate_result(
        self,
        command: str,
        success: bool,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            VerifyGateResult(
                command=command,
                success=success,
                summary=summary,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_review_gate_decision(
        self,
        decision: str,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            ReviewGateDecision(decision=decision, summary=summary, worker_id=worker_id, role=role)
        )

    def emit_error(
        self,
        code: str,
        message: str,
        recoverable: bool,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            ErrorRaised(
                code=code,
                message=message,
                recoverable=recoverable,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(WarningEmitted(message=message, worker_id=worker_id, role=role))

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(InfoEmitted(message=message, worker_id=worker_id, role=role))

    def emit_prompt_for_input(self, prompt_id: str, prompt_text: str, kind: str) -> None:
        self._emit_event(PromptForInput(prompt_id=prompt_id, prompt_text=prompt_text, kind=kind))

    def emit_config_form_request(self, form_id: str, schema: dict[str, Any]) -> None:
        self._emit_event(ConfigFormRequest(form_id=form_id, schema=schema))

    def on_status_update(self, status: StatusEvent) -> None:
        self.emit_status_update(mode=status.mode, model=status.model)

    def on_progress_update(self, message: str) -> None:
        self.emit_info(message)

    def on_assistant_token(self, delta: str) -> None:
        self.emit_message_delta(delta)

    def on_assistant_message_done(self, text: str) -> None:
        self.emit_message_end(text)

    def on_tool_start(self, event: ToolStartEvent) -> None:
        self.emit_tool_call_started(
            event.tool_call_id,
            event.name,
            json.dumps(event.args, ensure_ascii=True, sort_keys=True),
            worker_id=event.subagent_name,
            role=event.subagent_mode,
        )
        if self._semantic_activity_events:
            self.emit_activity(
                tool_activity(
                    call_id=event.tool_call_id,
                    name=event.name,
                    arguments=event.args,
                    status="running",
                    metadata={"step": event.step, "worker_id": event.subagent_name},
                )
            )

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        self.emit_tool_call_progress(
            event.tool_call_id,
            event.chunk,
            worker_id=event.subagent_name,
            role=event.subagent_mode,
        )

    def on_tool_end(self, event: ToolEndEvent) -> None:
        self.emit_tool_call_completed(
            event.tool_call_id,
            str(event.status).strip().lower() in {"completed", "done", "success"},
            json.dumps(
                {
                    "status": event.status,
                    "elapsed_ms": event.elapsed_ms,
                    "meta": sanitize_activity_metadata(event.meta),
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            worker_id=event.subagent_name,
            role=event.subagent_mode,
        )
        if self._semantic_activity_events:
            self.emit_activity(
                tool_activity(
                    call_id=event.tool_call_id,
                    name=event.name,
                    status=event.status,
                    duration_ms=event.elapsed_ms,
                    metadata={**event.meta, "worker_id": event.subagent_name},
                )
            )

    def on_patch_generated(self, event: PatchEvent) -> None:
        self.emit_activity(patch_activity(event))

    def on_warning(self, warning: str) -> None:
        self.emit_warning(warning)

    def on_error(self, err: str) -> None:
        self.emit_error("runtime_error", err, True)

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._approval_handler is not None:
            return self._approval_handler(request, self._emit_event)
        approval_id = str(request.metadata.get("approval_id") or uuid.uuid4().hex)
        approval_kind = str(request.kind or "generic")
        prompt_text = request.preview or request.reason or f"Approval required: {approval_kind}"
        warning = "Host-managed approval handling is unavailable; request denied fail-closed."
        self._emit_event(
            ProtocolPayloadEvent(
                "prompt_for_input",
                {
                    "prompt_id": approval_id,
                    "prompt_text": prompt_text,
                    "kind": "approval",
                    "approval_id": approval_id,
                    "approval_kind": approval_kind,
                    "reason": request.reason,
                    "preview": request.preview,
                    "files": list(request.files),
                    "command": request.command,
                    "metadata": dict(request.metadata),
                    "scope": None,
                    "allow_for_session_supported": False,
                    "allow_for_session_scope": None,
                    "allow_for_session_warning": warning,
                },
            )
        )
        self._emit_event(
            ProtocolPayloadEvent(
                "prompt_for_input",
                {
                    "kind": "approval_result",
                    "approval_id": approval_id,
                    "status": "denied",
                    "allow": False,
                    "allow_for_session": False,
                    "allow_for_session_supported": False,
                    "metadata": {
                        "status": "denied",
                        "allow": False,
                        "allow_for_session": False,
                        "allow_for_session_supported": False,
                    },
                },
            )
        )
        return ApprovalDecision(allow=False)
