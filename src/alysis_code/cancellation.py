"""Shared cooperative-cancellation primitives.

One token discipline for every long-running execution surface (IDE bridge
jobs, Forge swarm workers): a ``threading.Event`` is the single cancel
signal, a token wraps it for duck-typed checkpoints, and cancellation
surfaces as a dedicated exception type so callers can distinguish a
cooperative stop from a failure.

The agent turn engine (``agent/turn/core.py``) duck-types tokens: it calls
``throw_if_cancelled()`` when available, otherwise checks ``is_cancelled``.
Both behaviors are provided here.
"""

from __future__ import annotations

import threading


class CooperativeCancellationError(Exception):
    """Raised at a cooperative checkpoint after cancellation was requested."""

    def __init__(self, reason: str = "cancelled_by_user") -> None:
        super().__init__(reason)
        self.reason = str(reason or "cancelled_by_user")


class EventCancellationToken:
    """Duck-typed cancellation token backed by a ``threading.Event``."""

    error_class: type[CooperativeCancellationError] = CooperativeCancellationError

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self, reason: str = "cancelled_by_user") -> None:
        if self.is_cancelled:
            raise self.error_class(reason)
