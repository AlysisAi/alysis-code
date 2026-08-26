from __future__ import annotations

import json
from pathlib import Path

import pytest

from alysis_code.extensions import install as install_mod
from alysis_code.extensions.install import (
    PluginInstallError,
    disable_plugin,
    enable_plugin,
)
from alysis_code.extensions.models import ExtensionState, InstalledExtensionState
from alysis_code.extensions.state import load_global_state, load_project_overrides


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))


def _write_global_state(tmp_path: Path, *, enabled: bool = False) -> None:
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
                        "enabled": enabled,
                    }
                },
                "enabled": ["acme.demo"] if enabled else [],
            }
        ),
        encoding="utf-8",
    )


def _write_project_state(repo: Path, *, plugin_id: str = "acme.demo") -> None:
    path = repo / ".alysis" / "extensions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installed": {
                    plugin_id: {
                        "id": plugin_id,
                        "version": "1.2.3",
                        "commit": "a" * 40,
                        "enabled": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_enable_user_scope_on_installed_plugin_adds_enabled_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=False)

    result = enable_plugin(plugin_id="acme.demo", repo_root=tmp_path)

    state = load_global_state()
    assert result.no_op is False
    assert "acme.demo" in state.enabled
    assert state.installed["acme.demo"].enabled is True


def test_enable_user_scope_when_not_installed_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)

    with pytest.raises(PluginInstallError, match="plugin not installed"):
        enable_plugin(plugin_id="acme.demo", repo_root=tmp_path)


def test_enable_user_scope_already_enabled_is_noop_no_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=True)

    def fail_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("write should not happen")

    monkeypatch.setattr("alysis_code.extensions.state.atomic_write_json", fail_write)

    result = enable_plugin(plugin_id="acme.demo", repo_root=tmp_path)

    assert result.no_op is True
    assert result.previous_state == "enabled"


def test_disable_user_scope_removes_enabled_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=True)

    result = disable_plugin(plugin_id="acme.demo", repo_root=tmp_path)

    state = load_global_state()
    assert result.new_state == "disabled"
    assert "acme.demo" not in state.enabled
    assert state.installed["acme.demo"].enabled is False


def test_enable_project_scope_creates_overrides_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=False)
    repo = tmp_path / "repo"

    result = enable_plugin(plugin_id="acme.demo", repo_root=repo, project=True)

    assert result.scope == "project"
    assert (repo / ".alysis" / "extensions.json").exists()
    assert load_project_overrides(repo).enabled == ["acme.demo"]


def test_enable_project_scope_removes_from_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=False)
    repo = tmp_path / "repo"
    path = repo / ".alysis" / "extensions.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "disabled": ["acme.demo"]}))

    enable_plugin(plugin_id="acme.demo", repo_root=repo, project=True)

    overrides = load_project_overrides(repo)
    assert overrides.enabled == ["acme.demo"]
    assert overrides.disabled == []


def test_disable_project_scope_adds_disabled_and_removes_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=True)
    repo = tmp_path / "repo"
    path = repo / ".alysis" / "extensions.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "enabled": ["acme.demo"]}))

    disable_plugin(plugin_id="acme.demo", repo_root=repo, project=True)

    overrides = load_project_overrides(repo)
    assert overrides.enabled == []
    assert overrides.disabled == ["acme.demo"]


def test_project_enable_plugin_installed_only_user_scope_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=False)

    result = enable_plugin(plugin_id="acme.demo", repo_root=tmp_path / "repo", project=True)

    assert result.new_state == "enabled"


def test_project_enable_plugin_installed_only_project_scope_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _write_project_state(repo)

    result = enable_plugin(plugin_id="acme.demo", repo_root=repo, project=True)

    assert result.new_state == "enabled"
    assert load_project_overrides(repo).enabled == ["acme.demo"]


def test_project_override_atomic_failure_leaves_prior_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path, enabled=False)
    repo = tmp_path / "repo"
    path = repo / ".alysis" / "extensions.json"
    path.parent.mkdir(parents=True)
    original = {"schema_version": 1, "enabled": ["other.plugin"]}
    path.write_text(json.dumps(original), encoding="utf-8")

    def fail_write(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(install_mod, "atomic_write_json", fail_write)

    with pytest.raises(RuntimeError, match="disk full"):
        enable_plugin(plugin_id="acme.demo", repo_root=repo, project=True)

    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_state_round_trip_preserves_new_enabled_fields() -> None:
    state = ExtensionState(
        installed={"acme.demo": InstalledExtensionState(id="acme.demo", enabled=True)},
        enabled=["acme.demo"],
    )

    round_trip = ExtensionState.model_validate(state.model_dump(mode="json"))

    assert round_trip.installed["acme.demo"].enabled is True
    assert round_trip.enabled == ["acme.demo"]
