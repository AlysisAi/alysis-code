from __future__ import annotations

from alysis_code.agent_loop import _build_turn_language_system_message
from alysis_code.language_policy import (
    normalize_language_name,
    normalize_script_name,
)


def test_normalizers_keep_model_selected_names_without_alias_mapping() -> None:
    assert normalize_language_name("  Modern   Greek  ") == "Modern Greek"
    assert normalize_script_name("  Greek   alphabet  ") == "Greek alphabet"
    assert normalize_language_name("x" * 100) == "x" * 80
    assert normalize_script_name(None) == ""


def test_model_determined_language_directive_is_available_without_explicit_override() -> None:
    directive = _build_turn_language_system_message(
        "Greek",
        "Greek",
        explicit_language_override=False,
    )

    assert directive is not None
    assert "selected reply language/script for this turn is model-determined" in directive
    assert "Respond in Greek using the Greek writing system" in directive
