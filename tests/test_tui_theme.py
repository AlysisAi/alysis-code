from __future__ import annotations

from alysis_code.cli_impl.tui.app import _build_tui_style
from alysis_code.cli_impl.tui.setup_app import _build_setup_app_style, _build_setup_style


def _rules(theme: str) -> dict[str, str]:
    return dict(_build_tui_style(theme).style_rules)  # type: ignore[arg-type]


def test_light_theme_replaces_dark_chat_surfaces() -> None:
    rules = _rules("light")

    assert rules["tui.input"] == "#1f2328"
    assert "bg:#eef3ee" in rules["tui.transcript.userband"]
    assert "bg:#f6f8fa" in rules["tui.help"]
    assert "bg:#f6f8fa" in rules["completion-menu"]
    assert "bg:#f6f8fa" in rules["tui.config"]
    assert "#21262d" not in " ".join(rules.values())
    assert "#0d1117" not in " ".join(rules.values())


def test_dark_theme_keeps_the_existing_palette() -> None:
    rules = _rules("dark")

    assert rules["tui.input"] == "#e6edf3"
    assert "bg:#21262d" in rules["tui.transcript.userband"]
    assert "bg:#0d1117" in rules["tui.help"]
    assert "bg:#0d1117" in rules["tui.config"]


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
        "tui.config",
    )

    assert all("bg:" not in rules[name] for name in inherited_surface_rules)
    assert "ansigreen" in rules["tui.prompt"]


def test_setup_and_config_labels_follow_the_terminal_theme() -> None:
    light = dict(_build_setup_style("light").style_rules)  # type: ignore[arg-type]
    dark = dict(_build_setup_style("dark").style_rules)  # type: ignore[arg-type]

    assert light["setup.text"] == "#24292f"
    assert light["setup.dim"] == "#57606a"
    assert dark["setup.text"] == "#c9d1d9"
    assert dark["setup.dim"] == "#6e7681"

    standalone_light = dict(_build_setup_app_style("light").style_rules)  # type: ignore[arg-type]
    assert standalone_light["tui.config"] == "bg:#f6f8fa"
    assert standalone_light["setup.text"] == "#24292f"
