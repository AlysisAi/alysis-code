from __future__ import annotations

from alysis_code.cli_impl.tui.app import _build_tui_style


def _rules(theme: str) -> dict[str, str]:
    return dict(_build_tui_style(theme).style_rules)  # type: ignore[arg-type]


def test_light_theme_replaces_dark_chat_surfaces() -> None:
    rules = _rules("light")

    assert rules["tui.input"] == "#1f2328"
    assert "bg:#eef3ee" in rules["tui.transcript.userband"]
    assert "bg:#f6f8fa" in rules["tui.help"]
    assert "bg:#f6f8fa" in rules["completion-menu"]
    assert "#21262d" not in " ".join(rules.values())
    assert "#0d1117" not in " ".join(rules.values())


def test_dark_theme_keeps_the_existing_palette() -> None:
    rules = _rules("dark")

    assert rules["tui.input"] == "#e6edf3"
    assert "bg:#21262d" in rules["tui.transcript.userband"]
    assert "bg:#0d1117" in rules["tui.help"]


def test_neutral_theme_inherits_surface_backgrounds() -> None:
    rules = _rules("neutral")
    inherited_surface_rules = (
        "tui.input",
        "tui.transcript.userband",
        "tui.help",
        "completion-menu",
        "tui.picker",
        "tui.editor",
        "tui.approve",
        "tui.modal.scrim",
    )

    assert all("bg:" not in rules[name] for name in inherited_surface_rules)
    assert "ansigreen" in rules["tui.prompt"]
