from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code.cli import app as alysis_app
from alysis_code.config import (
    AppConfig,
    load_config,
    load_persisted_profile_keys,
    save_config,
)
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
    }


def _seed_profiles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="gpt-test")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1"))
    set_active_profile(cfg, "openai")
    save_config(cfg)


def _seed_subscription_profile(tmp_path: Path, monkeypatch) -> ProfileSpec:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-5.4",
        reasoning_effort="high",
    )
    cfg = AppConfig(model=profile.default_model)
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    save_config(cfg)
    return profile


def test_profile_list_shows_profiles_with_active_marker(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(alysis_app, ["profile", "list"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert "openai" in result.output
    assert "✓" in result.output


def test_profile_use_switches_active(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(alysis_app, ["profile", "use", "anthropic"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert load_config().extra_fields["active_profile"] == "anthropic"


def test_profile_use_unknown_errors(tmp_path: Path) -> None:
    result = CliRunner().invoke(alysis_app, ["profile", "use", "missing"], env=_env(tmp_path))

    assert result.exit_code == 2
    assert "Profile not found" in result.output


def test_profile_add_creates_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    result = CliRunner().invoke(
        alysis_app,
        ["profile", "add", "custom", "--base-url", "https://example.com/v1"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "custom" in load_config().extra_fields["profiles"]


def test_profile_add_accepts_safe_reasoning_trace_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "add",
            "custom",
            "--base-url",
            "https://example.com/v1",
            "--reasoning-trace-adapter",
            "openrouter_reasoning",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    profile = load_config().extra_fields["profiles"]["custom"]
    assert profile["reasoning_trace_adapter"] == "openrouter_reasoning"


def test_profile_add_cannot_overwrite_subscription_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _seed_subscription_profile(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "add",
            original.name,
            "--base-url",
            "https://example.com/v1",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "managed by the 'openai-codex' subscription connection" in result.output
    assert load_config().extra_fields["profiles"][original.name] == original.to_dict()


def test_profile_remove_with_yes_skips_confirm(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "remove", "anthropic", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "anthropic" not in load_config().extra_fields["profiles"]


def test_profile_preset_clones_into_named_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    result = CliRunner().invoke(
        alysis_app,
        ["profile", "preset", "anthropic", "--as", "claude"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    profile = load_config().extra_fields["profiles"]["claude"]
    assert profile["protocol"] == "anthropic_messages"
    assert profile["base_url"] == "https://api.anthropic.com/v1"
    assert profile["default_model"] == "claude-sonnet-5"
    assert profile["extra_headers"] == {}


def test_profile_preset_yes_cannot_overwrite_subscription_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _seed_subscription_profile(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "preset",
            "openai-responses",
            "--as",
            original.name,
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "cannot be overwritten through generic profile commands" in " ".join(
        result.output.split()
    )
    assert load_config().extra_fields["profiles"][original.name] == original.to_dict()


def test_profile_set_key_persists_per_profile(monkeypatch, tmp_path: Path) -> None:
    _seed_profiles(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "set-key", "anthropic", "--key", "sk-ant-test"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert load_persisted_profile_keys()["anthropic"] == "sk-ant-test"


def test_profile_presets_lists_known_presets(tmp_path: Path) -> None:
    result = CliRunner().invoke(alysis_app, ["profile", "presets"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert "anthropic" in result.output
    assert "compatibility" in result.output
    assert "native" in result.output
    assert "openai_responses" in result.output
    assert "openrouter" in result.output
    assert "qwen-us" in result.output


def test_qwen_us_preset_uses_dashscope_virginia_endpoint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "preset", "qwen-us"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    profile = load_config().extra_fields["profiles"]["qwen-us"]
    assert profile["base_url"] == "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
    assert profile["default_model"] == "qwen3.7-plus"


def test_profile_convert_to_native_updates_protocol_without_exposing_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _seed_profiles(tmp_path, monkeypatch)
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
            web_search_model="legacy-search-model",
        ),
    )
    save_config(cfg)
    set_key = CliRunner().invoke(
        alysis_app,
        ["profile", "set-key", "anthropic", "--key", "sk-ant-test"],
        env=_env(tmp_path),
    )
    assert set_key.exit_code == 0

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "convert", "anthropic", "--to", "native", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "anthropic_messages" in result.output
    assert "web_search_model" in result.output
    assert "legacy-search-model" in result.output
    assert "sk-ant-test" not in result.output
    profile = load_config().extra_fields["profiles"]["anthropic"]
    assert profile["protocol"] == "anthropic_messages"
    assert profile["base_url"] == "https://api.anthropic.com/v1"
    assert profile["web_search_adapter"] == "anthropic_messages"
    assert profile["web_search_model"] == ""
    assert load_persisted_profile_keys()["anthropic"] == "sk-ant-test"


def test_profile_convert_cannot_convert_subscription_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _seed_subscription_profile(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        alysis_app,
        [
            "profile",
            "convert",
            original.name,
            "--to",
            "compatibility",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "cannot be converted through generic profile commands" in " ".join(result.output.split())
    assert load_config().extra_fields["profiles"][original.name] == original.to_dict()


def test_profile_convert_to_compatibility_updates_gemini_native_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="gemini-3-flash-preview")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="gemini-native",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-3-flash-preview",
            web_search_adapter="gemini_grounding",
        ),
    )
    set_active_profile(cfg, "gemini-native")
    save_config(cfg)

    result = CliRunner().invoke(
        alysis_app,
        ["profile", "convert", "--to", "compatibility", "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    profile = load_config().extra_fields["profiles"]["gemini-native"]
    assert profile["protocol"] == "openai_compat"
    assert profile["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert profile["default_model"] == "gemini-3-flash-preview"
    assert profile["web_search_adapter"] == "gemini_grounding"
