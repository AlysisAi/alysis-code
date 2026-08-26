from __future__ import annotations

import json
import os
from pathlib import Path

from alysis_code.config import (
    load_config,
    resolve_profile_api_key,
    save_config,
    save_persisted_api_key,
)
from alysis_code.profiles import get_active_profile


def test_load_config_with_legacy_base_url_creates_default_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"base_url": "https://legacy.example/v1", "model": "old"}),
        encoding="utf-8",
    )

    profile = get_active_profile(load_config())

    assert profile.name == "default"
    assert profile.base_url == "https://legacy.example/v1"
    assert profile.default_model == "old"


def test_load_config_with_no_legacy_creates_openai_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))

    profile = get_active_profile(load_config())

    assert profile.name == "default"
    assert profile.base_url == "https://api.openai.com/v1"
    assert profile.api_key_env == "OPENAI_API_KEY"


def test_save_after_migration_persists_profiles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"model": "old"}), encoding="utf-8")

    cfg = load_config()
    save_config(cfg)

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["active_profile"] == "default"
    assert saved["profiles"]["default"]["base_url"] == "https://api.openai.com/v1"


def test_legacy_api_key_resolves_for_active_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    save_persisted_api_key("legacy-key")

    resolved = resolve_profile_api_key(load_config(), "default")

    assert resolved.key == "legacy-key"
    assert resolved.source == "stored:legacy"
