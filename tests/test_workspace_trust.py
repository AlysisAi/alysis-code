from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from alysis_code.extensions.activation import resolve_active_plugins
from alysis_code.extensions.workspace_trust import (
    WORKSPACE_TRUST_SCHEMA_VERSION,
    grant_workspace_trust,
    is_workspace_trusted,
    load_workspace_trust,
    workspace_trust_key,
    workspace_trust_path,
)


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))


def _write_global_enabled(tmp_path: Path, plugin_id: str = "acme.demo") -> None:
    path = tmp_path / "data" / "extensions" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installed": {plugin_id: {"id": plugin_id, "enabled": True}},
                "enabled": [plugin_id],
            }
        ),
        encoding="utf-8",
    )


def _write_overrides(repo: Path, payload: dict[str, object]) -> bytes:
    path = repo / ".alysis" / "extensions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_fresh_repo_no_overrides_resolver_does_not_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    _write_global_enabled(tmp_path)
    calls: list[object] = []

    decision = resolve_active_plugins(
        repo_root=tmp_path / "repo",
        workspace_trust_prompt=lambda request: calls.append(request) or True,
    )

    assert decision.enabled_plugin_ids == frozenset({"acme.demo"})
    assert calls == []
    assert is_workspace_trusted(repo_root=tmp_path / "repo", overrides_sha256="x") is False


def test_repo_with_overrides_no_trust_calls_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _write_overrides(repo, {"schema_version": 1, "enabled": ["acme.demo"]})
    calls: list[str] = []

    resolve_active_plugins(
        repo_root=repo,
        workspace_trust_prompt=lambda request: calls.append(request.overrides_sha256) or False,
    )

    assert len(calls) == 1


def test_prompt_accept_persists_trust_and_activates_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    raw = _write_overrides(repo, {"schema_version": 1, "enabled": ["acme.demo"]})

    decision = resolve_active_plugins(repo_root=repo, workspace_trust_prompt=lambda request: True)

    assert decision.enabled_plugin_ids == frozenset({"acme.demo"})
    assert decision.workspace_trust_granted is True
    assert is_workspace_trusted(
        repo_root=repo,
        overrides_sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_prompt_decline_ignores_project_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _write_overrides(repo, {"schema_version": 1, "enabled": ["acme.demo"]})

    decision = resolve_active_plugins(repo_root=repo, workspace_trust_prompt=lambda request: False)

    assert decision.enabled_plugin_ids == frozenset()
    assert decision.untrusted_project_plugin_ids == frozenset({"acme.demo"})


def test_overrides_modified_after_grant_reprompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    raw = _write_overrides(repo, {"schema_version": 1, "enabled": ["acme.demo"]})
    grant_workspace_trust(
        repo_root=repo,
        overrides_sha256=hashlib.sha256(raw).hexdigest(),
    )
    _write_overrides(repo, {"schema_version": 1, "enabled": ["acme.other"]})
    calls: list[str] = []

    resolve_active_plugins(
        repo_root=repo,
        workspace_trust_prompt=lambda request: calls.append(request.overrides_sha256) or False,
    )

    assert len(calls) == 1


def test_non_interactive_overrides_are_ignored_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _write_overrides(repo, {"schema_version": 1, "enabled": ["acme.demo"]})

    decision = resolve_active_plugins(repo_root=repo, workspace_trust_prompt=None)

    assert decision.enabled_plugin_ids == frozenset()
    assert decision.workspace_trust_was_prompted is False
    assert decision.untrusted_project_plugin_ids == frozenset({"acme.demo"})


def test_workspace_trust_file_is_schema_versioned_and_per_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"

    grant_workspace_trust(repo_root=repo_a, overrides_sha256="a" * 64)
    grant_workspace_trust(repo_root=repo_b, overrides_sha256="b" * 64)

    state = load_workspace_trust()
    assert state.schema_version == WORKSPACE_TRUST_SCHEMA_VERSION
    assert workspace_trust_path().exists()
    assert set(state.trusted) == {workspace_trust_key(repo_a), workspace_trust_key(repo_b)}
