from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from alysis_code.extensions.activation import resolve_active_plugins
from alysis_code.extensions.workspace_trust import grant_workspace_trust


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))


def _write_global_state(tmp_path: Path) -> None:
    path = tmp_path / "data" / "extensions" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installed": {
                    "acme.global": {"id": "acme.global", "enabled": True},
                    "acme.disabled": {"id": "acme.disabled", "enabled": True},
                    "acme.off": {"id": "acme.off", "enabled": False},
                },
                "enabled": ["acme.global", "acme.disabled"],
            }
        ),
        encoding="utf-8",
    )


def _write_overrides(repo: Path, *, enabled: list[str], disabled: list[str]) -> str:
    path = repo / ".alysis" / "extensions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {"schema_version": 1, "enabled": enabled, "disabled": disabled},
        sort_keys=True,
    ).encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def test_project_disable_wins_over_global_enable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path)
    repo = tmp_path / "repo"
    sha = _write_overrides(repo, enabled=[], disabled=["acme.disabled"])
    grant_workspace_trust(repo_root=repo, overrides_sha256=sha)

    decision = resolve_active_plugins(repo_root=repo, workspace_trust_prompt=None)

    assert "acme.disabled" not in decision.enabled_plugin_ids
    assert "acme.global" in decision.enabled_plugin_ids


def test_project_enable_adds_to_global_enabled_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path)
    repo = tmp_path / "repo"
    sha = _write_overrides(repo, enabled=["acme.off"], disabled=[])
    grant_workspace_trust(repo_root=repo, overrides_sha256=sha)

    decision = resolve_active_plugins(repo_root=repo, workspace_trust_prompt=None)

    assert "acme.off" in decision.enabled_plugin_ids


def test_user_enabled_project_disabled_not_active_in_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path)
    repo = tmp_path / "repo"
    sha = _write_overrides(repo, enabled=[], disabled=["acme.global"])
    grant_workspace_trust(repo_root=repo, overrides_sha256=sha)

    decision = resolve_active_plugins(repo_root=repo, workspace_trust_prompt=None)

    assert "acme.global" not in decision.enabled_plugin_ids


def test_project_enable_wins_when_same_id_in_enabled_and_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path)
    repo = tmp_path / "repo"
    sha = _write_overrides(repo, enabled=["acme.off"], disabled=["acme.off"])
    grant_workspace_trust(repo_root=repo, overrides_sha256=sha)

    decision = resolve_active_plugins(repo_root=repo, workspace_trust_prompt=None)

    assert "acme.off" in decision.enabled_plugin_ids


def test_prompt_request_lists_added_and_removed_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path)
    repo = tmp_path / "repo"
    _write_overrides(repo, enabled=["acme.off"], disabled=["acme.global"])
    seen: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    resolve_active_plugins(
        repo_root=repo,
        workspace_trust_prompt=lambda request: (
            seen.append((request.plugins_added, request.plugins_removed)) or False
        ),
    )

    assert seen == [(("acme.off",), ("acme.global",))]


def test_trusted_empty_override_lists_do_not_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_state(tmp_path)
    repo = tmp_path / "repo"
    _write_overrides(repo, enabled=[], disabled=[])
    calls: list[object] = []

    decision = resolve_active_plugins(
        repo_root=repo,
        workspace_trust_prompt=lambda request: calls.append(request) or True,
    )

    assert calls == []
    assert decision.enabled_plugin_ids == frozenset({"acme.global", "acme.disabled"})
