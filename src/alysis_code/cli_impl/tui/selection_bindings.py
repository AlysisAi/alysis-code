"""Shared text-selection editing behavior for prompt_toolkit applications."""

from __future__ import annotations

from typing import Any

from prompt_toolkit.filters import has_selection, is_multiline, is_read_only
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys


def selection_editing_bindings() -> KeyBindings:
    """Return normal input-field behavior for a selection in the current buffer."""
    bindings = KeyBindings()
    editable_selection = has_selection & ~is_read_only

    @bindings.add("backspace", filter=editable_selection, eager=True)
    def _cut_selection(event: Any) -> None:
        data = event.current_buffer.cut_selection()
        event.app.clipboard.set_data(data)

    @bindings.add(Keys.Any, filter=editable_selection)
    def _replace_selection(event: Any) -> None:
        event.current_buffer.cut_selection()
        event.current_buffer.insert_text(event.data)

    @bindings.add(
        "c-j",
        filter=editable_selection & is_multiline,
        eager=True,
    )
    def _replace_selection_with_newline(event: Any) -> None:
        event.current_buffer.cut_selection()
        event.current_buffer.insert_text("\n")

    return bindings


__all__ = ["selection_editing_bindings"]
