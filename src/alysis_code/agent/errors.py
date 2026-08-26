from __future__ import annotations

from typing import Any


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        *args: object,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        self.result_payload = result_payload
        super().__init__(*args)


class ApprovalDeclinedError(AgentRuntimeError):
    def __init__(
        self,
        approval_kind: str,
        *,
        message: str | None = None,
    ) -> None:
        self.approval_kind = str(approval_kind or "approval").strip() or "approval"
        super().__init__(message or f"User declined: {self.approval_kind}")


class SessionWorkdirError(AgentRuntimeError):
    pass
