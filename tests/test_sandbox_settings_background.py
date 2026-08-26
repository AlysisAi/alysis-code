from __future__ import annotations

import pytest

from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_settings import resolve_shell_sandbox_settings


def _plain_cfg() -> AppConfig:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {}
    return cfg


def test_background_settings_defaults() -> None:
    settings = resolve_shell_sandbox_settings(_plain_cfg())

    assert settings.background_max_concurrent == 4
    assert settings.background_output_max_lines == 2000
    assert settings.background_output_max_bytes == 256 * 1024
    assert settings.background_kill_timeout_s == 10.0


def test_background_settings_from_extra_fields() -> None:
    cfg = _plain_cfg()
    cfg.extra_fields = {
        "shell_sandbox": {
            "background_max_concurrent": "8",
            "background_output_max_lines": "300",
            "background_output_max_bytes": "4096",
            "background_kill_timeout_s": "0.5",
        }
    }

    settings = resolve_shell_sandbox_settings(cfg)

    assert settings.background_max_concurrent == 8
    assert settings.background_output_max_lines == 300
    assert settings.background_output_max_bytes == 4096
    assert settings.background_kill_timeout_s == 0.5


def test_background_settings_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _plain_cfg()
    cfg.extra_fields = {
        "shell_sandbox": {
            "background_max_concurrent": 2,
            "background_output_max_lines": 20,
            "background_output_max_bytes": 200,
            "background_kill_timeout_s": 5.0,
        }
    }
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKGROUND_MAX_CONCURRENT", "9")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_LINES", "99")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_BYTES", "999")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKGROUND_KILL_TIMEOUT_S", "1.25")

    settings = resolve_shell_sandbox_settings(cfg)

    assert settings.background_max_concurrent == 9
    assert settings.background_output_max_lines == 99
    assert settings.background_output_max_bytes == 999
    assert settings.background_kill_timeout_s == 1.25


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf"])
def test_invalid_background_kill_timeout_raises(value: str) -> None:
    cfg = _plain_cfg()
    cfg.extra_fields = {"shell_sandbox": {"background_kill_timeout_s": value}}

    with pytest.raises(ConfigError):
        resolve_shell_sandbox_settings(cfg)


@pytest.mark.parametrize("value", ["0", "abc"])
def test_invalid_background_max_concurrent_raises(value: str) -> None:
    cfg = _plain_cfg()
    cfg.extra_fields = {"shell_sandbox": {"background_max_concurrent": value}}

    with pytest.raises(ConfigError):
        resolve_shell_sandbox_settings(cfg)
