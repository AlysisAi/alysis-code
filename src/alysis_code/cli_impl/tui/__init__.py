"""Full-screen TUI for ``alysis``.

The TUI is the default interactive chat surface. Set ``ALYSIS_TUI=0`` to
fall back to the classic terminal chat temporarily.
"""

from __future__ import annotations

from .config import is_tui_enabled
from .state import EXEC_MODE_CYCLE, TuiState, next_exec_mode

__all__ = [
    "is_tui_enabled",
    "TuiState",
    "run_tui",
    "EXEC_MODE_CYCLE",
    "next_exec_mode",
]


def run_tui(  # noqa: D401,A002 - thin lazy wrapper
    state: TuiState,
    *,
    owl_color: bool = True,
    input=None,
    output=None,
    session_builder=None,
    on_turn_complete=None,
    command_runner=None,
    background_turns: bool = True,
    help_sections=None,
    panel_providers=None,
    **kwargs,
):
    """Lazily import and run the TUI app.

    This keeps prompt_toolkit app cost off the import path until interactive
    chat actually starts.

    Forwards every public ``app.run_tui`` argument; ``**kwargs`` passes through
    any newer ones (e.g. command-presentation hooks) without re-touching this
    wrapper each time a command primitive is added.
    """
    from .app import run_tui as _run_tui

    return _run_tui(
        state,
        owl_color=owl_color,
        input=input,
        output=output,
        session_builder=session_builder,
        on_turn_complete=on_turn_complete,
        command_runner=command_runner,
        background_turns=background_turns,
        help_sections=help_sections,
        panel_providers=panel_providers,
        **kwargs,
    )
