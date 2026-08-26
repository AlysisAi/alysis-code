from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.extensions.install import (
    ComponentInstallSummary,
    PermissionsSummary,
    PluginInstallError,
    PluginInstallResult,
    PluginUninstallResult,
    TrustPromptRequest,
)
from alysis_code.extensions.registry import RegistryFile

COMMIT = "a" * 40


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "config"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _request() -> TrustPromptRequest:
    return TrustPromptRequest(
        plugin_id="acme.demo",
        plugin_name="Demo Plugin",
        version="1.2.3",
        description="Demo plugin",
        source_url="https://example.com/acme/demo.git",
        commit=COMMIT,
        manifest_sha256="f" * 64,
        components=ComponentInstallSummary(
            skill_ids=("demo-skill",),
            tool_ids=("demo_tool",),
            mcp_server_ids=("demo_server",),
            hook_ids=("start",),
        ),
        permissions_summary=PermissionsSummary(
            network=True,
            filesystem_write=True,
            required_env=("TOKEN",),
            mcp_scopes=("tools",),
            hook_events=("SessionStart",),
        ),
        security=None,
        is_reinstall_with_new_commit=False,
    )


def _result(*, prompted: bool = True) -> PluginInstallResult:
    return PluginInstallResult(
        plugin_id="acme.demo",
        version="1.2.3",
        commit=COMMIT,
        manifest_sha256="f" * 64,
        scope="user",
        components_installed=ComponentInstallSummary(("demo-skill",), ("demo_tool",), (), ()),
        trust_was_prompted=prompted,
    )


def test_cli_ext_install_prompts_and_accepts(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    seen: list[bool] = []

    def fake_install_plugin(**kwargs: object) -> PluginInstallResult:
        trust_prompt = kwargs["trust_prompt"]
        assert callable(trust_prompt)
        seen.append(bool(trust_prompt(_request())))
        return _result()

    monkeypatch.setattr(cli_mod, "install_plugin", fake_install_plugin)

    result = runner.invoke(
        alysis_app, ["ext", "install", "acme.demo"], input="y\n", env=_env(tmp_path)
    )

    assert result.exit_code == 0
    assert seen == [True]
    assert "Plugin install trust request" in result.output
    assert "Installed plugin acme.demo" in result.output


def test_cli_ext_install_prompt_rejected_exits_1(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    def fake_install_plugin(**kwargs: object) -> PluginInstallResult:
        trust_prompt = kwargs["trust_prompt"]
        if not trust_prompt(_request()):
            raise PluginInstallError("install rejected by user")
        return _result()

    monkeypatch.setattr(cli_mod, "install_plugin", fake_install_plugin)

    result = runner.invoke(
        alysis_app, ["ext", "install", "acme.demo"], input="n\n", env=_env(tmp_path)
    )

    assert result.exit_code == 1
    assert "install rejected by user" in result.output


def test_cli_ext_install_yes_does_not_prompt(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        cli_mod.typer,
        "confirm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("prompted")),
    )

    def fake_install_plugin(**kwargs: object) -> PluginInstallResult:
        assert kwargs["trust_prompt"](_request()) is True
        return _result()

    monkeypatch.setattr(cli_mod, "install_plugin", fake_install_plugin)

    result = runner.invoke(alysis_app, ["ext", "install", "acme.demo", "--yes"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert "Plugin install trust request" in result.output


def test_cli_ext_install_ci_env_accepts_silently(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        cli_mod.typer,
        "confirm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("prompted")),
    )

    def fake_install_plugin(**kwargs: object) -> PluginInstallResult:
        assert kwargs["trust_prompt"](_request()) is True
        return _result()

    monkeypatch.setattr(cli_mod, "install_plugin", fake_install_plugin)
    env = _env(tmp_path)
    env["ALYSIS_CI"] = "1"

    result = runner.invoke(alysis_app, ["ext", "install", "acme.demo"], env=env)

    assert result.exit_code == 0
    assert "Plugin install trust request" not in result.output


def test_cli_ext_uninstall_prompts_before_destructive_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    def fake_uninstall_plugin(**kwargs: object):
        calls.append(str(kwargs["plugin_id"]))
        return PluginUninstallResult(
            plugin_id="acme.demo",
            scope="user",
            components_removed=ComponentInstallSummary((), (), (), ()),
        )

    monkeypatch.setattr(cli_mod, "uninstall_plugin", fake_uninstall_plugin)

    result = runner.invoke(
        alysis_app,
        ["ext", "uninstall", "acme.demo"],
        input="y\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert calls == ["acme.demo"]
    assert "Uninstalled plugin acme.demo" in result.output


def test_cli_ext_uninstall_not_installed_exits_1(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    def fake_uninstall_plugin(**kwargs: object):
        raise PluginInstallError("plugin not installed in project: acme.demo")

    monkeypatch.setattr(cli_mod, "uninstall_plugin", fake_uninstall_plugin)

    result = runner.invoke(
        alysis_app,
        ["ext", "uninstall", "acme.demo", "--project", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "plugin not installed in project" in result.output


def test_cli_ext_info_shows_install_record_fields(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    env = _env(tmp_path)
    state_path = tmp_path / "data" / "extensions" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installed": {
                    "acme.demo": {
                        "id": "acme.demo",
                        "version": "1.2.3",
                        "commit": COMMIT,
                        "manifest_sha256": "f" * 64,
                        "installed_at": "2026-05-01T00:00:00+00:00",
                        "source_url": "https://example.com/acme/demo.git",
                        "scope": "user",
                        "component_ids": {"tool": ["demo_tool"]},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "load_registry", lambda: RegistryFile(extensions=[]))

    result = runner.invoke(alysis_app, ["ext", "info", "acme.demo"], env=env)

    assert result.exit_code == 0
    assert "manifest_sha256" in result.output
    assert "installed_at" in result.output
    assert "demo_tool" in result.output
