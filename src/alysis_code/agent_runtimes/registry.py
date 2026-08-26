from __future__ import annotations

from collections.abc import Iterable

from .base import AgentRuntimeAdapter


class RuntimeRegistryError(ValueError):
    """Base error for invalid delegated-runtime registry operations."""


class DuplicateRuntimeError(RuntimeRegistryError):
    """Raised when two adapters claim the same runtime identifier."""


class UnknownRuntimeError(RuntimeRegistryError):
    """Raised when a configured runtime identifier is not registered."""


class AgentRuntimeRegistry:
    """Explicit registry for provider-managed runtime adapters."""

    def __init__(self, adapters: Iterable[AgentRuntimeAdapter] = ()) -> None:
        self._adapters: dict[str, AgentRuntimeAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: AgentRuntimeAdapter) -> None:
        runtime_id = _runtime_id(adapter.runtime_id)
        if runtime_id in self._adapters:
            raise DuplicateRuntimeError(f"Agent runtime {runtime_id!r} is already registered.")
        self._adapters[runtime_id] = adapter

    def get(self, runtime_id: str) -> AgentRuntimeAdapter:
        normalized = _runtime_id(runtime_id)
        try:
            return self._adapters[normalized]
        except KeyError as exc:
            available = ", ".join(self.runtime_ids()) or "none"
            raise UnknownRuntimeError(
                f"Unknown agent runtime {normalized!r}. Available runtimes: {available}."
            ) from exc

    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapters(self) -> tuple[AgentRuntimeAdapter, ...]:
        return tuple(self._adapters[runtime_id] for runtime_id in self.runtime_ids())

    def __contains__(self, runtime_id: object) -> bool:
        if not isinstance(runtime_id, str):
            return False
        normalized = runtime_id.strip()
        return bool(normalized) and normalized in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)


def _runtime_id(value: object) -> str:
    runtime_id = str(value or "").strip()
    if not runtime_id:
        raise RuntimeRegistryError("Agent runtime id must be a non-empty string.")
    return runtime_id
