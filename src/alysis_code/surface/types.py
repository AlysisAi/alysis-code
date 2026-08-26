from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StatusEvent:
    mode: str
    model: str
    workspace: str
    session_id: str
    branch: str
    dirty: bool
    stream: bool
    task: str = "-"


@dataclass(frozen=True, slots=True)
class ToolStartEvent:
    tool_call_id: str
    name: str
    args: dict[str, Any]
    step: int
    subagent_name: str | None = None
    subagent_mode: str | None = None
    nesting_depth: int = 0


@dataclass(frozen=True, slots=True)
class ToolOutputEvent:
    tool_call_id: str
    name: str
    chunk: str
    subagent_name: str | None = None
    subagent_mode: str | None = None
    nesting_depth: int = 0


@dataclass(frozen=True, slots=True)
class ToolEndEvent:
    tool_call_id: str
    name: str
    status: str
    elapsed_ms: int
    meta: dict[str, Any] = field(default_factory=dict)
    subagent_name: str | None = None
    subagent_mode: str | None = None
    nesting_depth: int = 0


@dataclass(frozen=True, slots=True)
class SubagentStartEvent:
    name: str
    mode: str
    subagent_session_id: str | None = None
    # What the subagent is for (the definition's description). Surfaces may show a
    # condensed form so the user knows what agent they just entered.
    description: str = ""
    subagent_run_id: str | None = None
    workspace_view: str = "shared"
    label: str = ""


@dataclass(frozen=True, slots=True)
class SubagentEndEvent:
    name: str
    mode: str
    status: str
    elapsed_ms: int
    steps_completed: int
    subagent_session_id: str | None = None
    error: str | None = None
    subagent_run_id: str | None = None
    workspace_view: str = "shared"
    label: str = ""


@dataclass(frozen=True, slots=True)
class PatchEvent:
    files: list[str]
    diff: str
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    kind: str
    reason: str
    preview: str
    files: list[str] = field(default_factory=list)
    command: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    allow_for_session_scope: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    allow: bool
    allow_for_session: bool = False
