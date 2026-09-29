"""IDE keys override stale CLI keys only for the named provider; no network calls."""

import json
from pathlib import Path

import pytest

from alysis_code.config import (
    AppConfig,
    credentials_path,
    resolve_api_key,
    resolve_profile_api_key,
    save_persisted_profile_key,
)
from alysis_code.profiles import ProfileSpec, add_profile


def ide_env(profile: str) -> str:
    return f"ALYSIS_IDE_PROFILE_{profile.encode('utf-8').hex().upper()}_API_KEY"


@pytest.mark.parametrize("profile", ["default", "openai-responses", "custom_openai"])
def test_ide_key_overrides_stored_profile_without_rewriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-other-key")
    monkeypatch.setenv("ALYSIS_API_KEY", "legacy-generic-key")
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": profile}
    add_profile(
        cfg,
        ProfileSpec(
            name=profile, base_url="https://api.openai.com/v1/", api_key_env="OPENAI_API_KEY"
        ),
    )
    save_persisted_profile_key(profile, "previous-cli-key")
    original = credentials_path().read_bytes()
    monkeypatch.setenv(ide_env(profile), " new-vscode-key ")

    for resolved in [resolve_api_key(cfg), resolve_profile_api_key(cfg, profile)]:
        assert resolved.key == "new-vscode-key"
        assert resolved.source == f"env:{ide_env(profile)}"
    assert credentials_path().read_bytes() == original

    monkeypatch.delenv(ide_env(profile))
    assert resolve_api_key(cfg).key == "previous-cli-key"
    assert "new-vscode-key" not in json.dumps(cfg.model_dump())


def test_ide_keys_are_isolated_when_profiles_share_openai_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": "one"}
    for name in ["one", "two"]:
        add_profile(
            cfg,
            ProfileSpec(
                name=name, base_url="https://api.openai.com/v1/", api_key_env="OPENAI_API_KEY"
            ),
        )
        monkeypatch.setenv(ide_env(name), f"key-for-{name}")
    for name in ["one", "two"]:
        assert resolve_api_key(cfg, profile_name=name).key == f"key-for-{name}"
        assert resolve_profile_api_key(cfg, name).key == f"key-for-{name}"
    monkeypatch.setenv(ide_env("absent"), "not-a-configured-provider")
    assert resolve_profile_api_key(cfg, "absent").key is None


def test_empty_ide_key_retains_cli_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": "openai-responses"}
    add_profile(cfg, ProfileSpec(name="openai-responses", base_url="https://api.openai.com/v1/"))
    save_persisted_profile_key("openai-responses", "previous-cli-key")
    monkeypatch.setenv(ide_env("openai-responses"), "   ")
    assert resolve_api_key(cfg).key == "previous-cli-key"


def test_ide_key_is_redacted_by_existing_environment_secret_rules() -> None:
    from alysis_code.logging_redaction import SecretRedactor

    secret = "test-credential-7fb80c81a1b64a53943c"
    redactor = SecretRedactor({ide_env("openai-responses"): secret})
    assert secret not in redactor.redact(f"Provider failed using {secret}")
