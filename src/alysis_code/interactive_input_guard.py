from __future__ import annotations

import threading
from contextlib import contextmanager

_ACTIVE_PROMPT_DEPTH = 0
_ACTIVE_PROMPT_TERMINAL_OWNER_DEPTH = 0
_ACTIVE_PROMPT_LOCK = threading.Lock()
_ACTIVE_PROMPT_EVENT = threading.Event()


def is_interactive_prompt_active() -> bool:
    return _ACTIVE_PROMPT_EVENT.is_set()


def is_interactive_prompt_terminal_owner() -> bool:
    with _ACTIVE_PROMPT_LOCK:
        return _ACTIVE_PROMPT_TERMINAL_OWNER_DEPTH > 0


@contextmanager
def interactive_prompt_guard(*, owns_terminal: bool = False) -> None:
    global _ACTIVE_PROMPT_DEPTH, _ACTIVE_PROMPT_TERMINAL_OWNER_DEPTH
    with _ACTIVE_PROMPT_LOCK:
        _ACTIVE_PROMPT_DEPTH += 1
        if owns_terminal:
            _ACTIVE_PROMPT_TERMINAL_OWNER_DEPTH += 1
        _ACTIVE_PROMPT_EVENT.set()
    try:
        yield
    finally:
        with _ACTIVE_PROMPT_LOCK:
            _ACTIVE_PROMPT_DEPTH = max(0, _ACTIVE_PROMPT_DEPTH - 1)
            if owns_terminal:
                _ACTIVE_PROMPT_TERMINAL_OWNER_DEPTH = max(
                    0, _ACTIVE_PROMPT_TERMINAL_OWNER_DEPTH - 1
                )
            if _ACTIVE_PROMPT_DEPTH == 0:
                _ACTIVE_PROMPT_EVENT.clear()
