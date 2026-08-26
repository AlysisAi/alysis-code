from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.extensions.install import EnableResult, PluginInstallError
from alysis_code.extensions.registry import RegistryFile


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _write_state(tmp_path: Path, *, enabled: bool = False) -> None:
    path = tmp_path / "data" / "extensions" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installed": {
                    "acme.demo": {
                        "id": "acme.demo",
                        "version": "1.2.3",
                        "commit": "a" * 40,
                        "manifest_sha256": "f" * 64,
                        "installed_at": "2026-05-01T00:00:00+00:00",
                        "enabled": enabled,
                    }
                },
                "enabled": ["acme.demo"] if enabled else [],
            }
        ),
        encoding="utf-8",
    )


def test_cli_ext_enable_exit_0_prints_confirmation(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    def fake_enable_plugin(**kwargs: object) -> EnableResult:
        return EnableResult(
            plugin_id=str(kwargs["plugin_id"]),
            scope="user",
            previous_state="disabled",
            new_state="enabled",
            no_op=False,
        )

    monkeypatch.setattr(cli_mod, "enable_plugin", fake_enable_plugin)

    result = runner.invoke(
        alysis_app,
        ["ext", "enable", "acme.demo", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "acme.demo is enabled in user scope" in result.output


def test_cli_ext_enable_not_installed_exit_1(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        cli_mod,
        "enable_plugin",
        lambda **kwargs: (_ for _ in ()).throw(PluginInstallError("plugin not installed")),
    )

    result = runner.invoke(
        alysis_app,
        ["ext", "enable", "missing.plugin", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "plugin not installed" in result.output


def test_cli_ext_disable_after_enable_round_trip(tmp_path: Path) -> None:
    runner = CliRunner()
    _write_state(tmp_path, enabled=True)

    result = runner.invoke(
        alysis_app,
        ["ext", "disable", "acme.demo", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    state = json.loads((tmp_path / "data" / "extensions" / "state.json").read_text())
    assert state["enabled"] == []
    assert state["installed"]["acme.demo"]["enabled"] is False


def test_cli_ext_enable_project_writes_project_file(tmp_path: Path) -> None:
    runner = CliRunner()
    _write_state(tmp_path, enabled=False)
    repo = tmp_path / "repo"

    result = runner.invoke(
        alysis_app,
        ["ext", "enable", "acme.demo", "--project", "--path", os.fspath(repo), "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    raw = json.loads((repo / ".alysis" / "extensions.json").read_text())
    assert raw["enabled"] == ["acme.demo"]


def test_cli_ext_info_shows_enabled_scope_and_effective_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    _write_state(tmp_path, enabled=True)
    monkeypatch.setattr(cli_mod, "load_registry", lambda: RegistryFile(extensions=[]))

    result = runner.invoke(alysis_app, ["ext", "info", "acme.demo"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert "installed scopes" in result.output
    assert "enabled (user)" in result.output
    assert "enabled (effective)" in result.output


def test_cli_ext_info_shows_workspace_trust_status_when_overrides_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    _write_state(tmp_path, enabled=True)
    repo = tmp_path / "repo"
    overrides = repo / ".alysis" / "extensions.json"
    overrides.parent.mkdir(parents=True, exist_ok=True)
    overrides.write_text(json.dumps({"schema_version": 1, "enabled": ["acme.demo"]}))
    monkeypatch.setattr(cli_mod, "load_registry", lambda: RegistryFile(extensions=[]))

    result = runner.invoke(
        alysis_app,
        ["ext", "info", "acme.demo", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "workspace trust" in result.output
    assert "untrusted" in result.output


def test_cli_ext_enable_prompts_and_accepts(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    calls: list[str] = []

    def fake_enable_plugin(**kwargs: object) -> EnableResult:
        calls.append(str(kwargs["plugin_id"]))
        return EnableResult(
            plugin_id="acme.demo",
            scope="user",
            previous_state="disabled",
            new_state="enabled",
            no_op=False,
        )

    monkeypatch.setattr(cli_mod, "enable_plugin", fake_enable_plugin)

    result = runner.invoke(
        alysis_app,
        ["ext", "enable", "acme.demo"],
        input="y\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert calls == ["acme.demo"]
