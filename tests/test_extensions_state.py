from __future__ import annotations

import json
from pathlib import Path

import pytest

from alysis_code.extensions.models import ExtensionState, ProjectExtensionOverrides
from alysis_code.extensions.state import (
    compute_effective_enabled,
    load_global_state,
    load_project_overrides,
)


def test_load_missing_global_and_project_state_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))

    global_state = load_global_state()
    project_overrides = load_project_overrides(tmp_path / "repo")

    assert global_state.installed == {}
    assert global_state.enabled == []
    assert project_overrides.enabled == []
    assert project_overrides.disabled == []
    assert compute_effective_enabled(global_state, project_overrides) == set()


def test_compute_effective_enabled_applies_project_disable_then_enable() -> None:
    global_state = ExtensionState(
        enabled=["ext.one", "ext.two"],
        installed={
            "ext.three": {"enabled": True},
            "ext.four": {"enabled": False},
        },
    )
    project_overrides = ProjectExtensionOverrides(
        disabled=["ext.two", "ext.five"],
        enabled=["ext.four", "ext.five"],
    )

    effective = compute_effective_enabled(global_state, project_overrides)
    assert effective == {"ext.one", "ext.three", "ext.four", "ext.five"}


def test_state_models_preserve_unknown_keys_when_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    repo_root = tmp_path / "repo"
    (data_dir / "extensions").mkdir(parents=True, exist_ok=True)
    (repo_root / ".alysis").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(data_dir))

    (data_dir / "extensions" / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": ["ext.one"],
                "unknown_global": {"x": 1},
            }
        ),
        encoding="utf-8",
    )
    (repo_root / ".alysis" / "extensions.json").write_text(
        json.dumps(
            {
                "enabled": ["ext.two"],
                "unknown_project": True,
            }
        ),
        encoding="utf-8",
    )

    global_state = load_global_state()
    project_overrides = load_project_overrides(repo_root)
    assert global_state.model_extra is not None
    assert project_overrides.model_extra is not None
    assert global_state.model_extra.get("unknown_global") == {"x": 1}
    assert project_overrides.model_extra.get("unknown_project") is True
