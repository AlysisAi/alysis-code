from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import AgentRuntimeSettings


@dataclass(frozen=True, slots=True)
class AuthMethod:
    """One authentication method exposed by a provider-managed runtime."""

    id: str
    label: str
    provider_managed: bool = True
    interactive: bool = True


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Stable capability flags used before a delegated runtime is selected."""

    streaming: bool = False
    session_resume: bool = False
    image_inputs: bool = False
    structured_output: bool = False
    read_only: bool = True
    workspace_write: bool = True


@dataclass(frozen=True, slots=True)
class RuntimeProbeStatus:
    """Result of checking whether a runtime is installed and usable."""

    available: bool
    executable: str | None = None
    version: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAccountStatus:
    """Provider-owned account state without exposing provider credentials."""

    authenticated: bool
    verified: bool = True
    auth_method_id: str | None = None
    account_label: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTurnRequest:
    """Provider-neutral input for one delegated agent turn."""

    prompt: str
    cwd: Path
    mode: Literal["readonly", "review", "auto", "fullaccess"] = "review"
    session_id: str | None = None
    images: tuple[Path, ...] = ()
    no_log: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeTurnResult:
    """In-memory result of a delegated turn; provider credentials are never included."""

    runtime_id: str
    command: tuple[str, ...]
    exit_code: int
    final_message: str = ""
    session_id: str | None = None
    stdout: str = ""
    stderr: str = ""
    events: tuple[Mapping[str, object], ...] = ()
    usage: Mapping[str, object] | None = None
    timed_out: bool = False
    error: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


@runtime_checkable
class AgentRuntimeAdapter(Protocol):
    """Discovery contract implemented by each delegated runtime adapter."""

    runtime_id: str
    display_name: str
    capabilities: RuntimeCapabilities
    auth_methods: tuple[AuthMethod, ...]

    def probe(self, settings: AgentRuntimeSettings) -> RuntimeProbeStatus: ...

    def account_status(self, settings: AgentRuntimeSettings) -> RuntimeAccountStatus: ...

    def login(
        self,
        settings: AgentRuntimeSettings,
        method_id: str,
    ) -> RuntimeAccountStatus: ...

    def logout(self, settings: AgentRuntimeSettings) -> RuntimeAccountStatus: ...

    def run_turn(
        self,
        settings: AgentRuntimeSettings,
        request: RuntimeTurnRequest,
    ) -> RuntimeTurnResult: ...
