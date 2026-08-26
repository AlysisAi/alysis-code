from __future__ import annotations

import os
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl.commands import root as root_mod
from alysis_code.config import AppConfig, save_config
from alysis_code.llm.types import LLMResponse, LLMUsage
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_diagnostics import (
    ProviderLiveValidation,
    ReasoningSuppressionReport,
    WebSearchLiveValidation,
    build_provider_diagnostics,
    probe_reasoning_suppression_live,
    validate_active_provider_live,
    validate_web_search_live,
)


@pytest.fixture(autouse=True)
def _clear_generic_web_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_API_KEY", raising=False)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "TAVILY_API_KEY": "",
    }


def _cfg_with_profile(
    profile: ProfileSpec, *, stream: bool = False, web_search_mode: str = "auto"
) -> AppConfig:
    cfg = AppConfig(
        model=profile.default_model or "test-model", stream=stream, web_search_mode=web_search_mode
    )
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def _render_provider_table(cfg: AppConfig) -> str:
    console = Console(record=True, width=180)
    console.print(root_mod._provider_doctor_table(cfg))
    return console.export_text()


def test_provider_diagnostics_redacts_api_key_and_shows_native_vs_compat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
        ),
        web_search_mode="native",
    )

    output = _render_provider_table(cfg)

    assert "anthropic" in output
    assert "anthropic_messages" in output
    assert "native" in output
    assert "api.anthropic.com" in output
    assert "https://api.anthropic.com/v1" not in output
    assert "sk-ant-secret-value" not in output
    assert "env:ANTHROPIC_API_KEY (redacted)" in output
    assert "provider_hosted" not in output
    assert "native/provider-hosted" in output


def test_provider_diagnostics_shows_compatibility_protocol_and_external_search(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-value")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol="openai_compat",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="tavily",
        ),
        web_search_mode="external",
    )

    output = _render_provider_table(cfg)

    assert "openai_compat" in output
    assert "compatibility" in output
    assert "external" in output
    assert "api.openai.com" in output
    assert "sk-openai-secret-value" not in output
    assert "tvly-secret-value" not in output


def test_provider_diagnostics_surfaces_effective_cache_policy() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="openai_responses",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)
    rows = dict(diagnostics.rows())

    assert rows["cache_status"] == "available"
    assert rows["cache_strategy"] == "openai_prompt_cache"
    assert rows["cache_capability_source"] in {"preset", "protocol"}
    assert "prompt_cache_key" in rows["cache_allowed_fields"]
    assert rows["cache_emitted_fields"] == "none"


def test_provider_diagnostics_accepts_auth_backed_responses_endpoint() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="gpt-5.4",
        ),
        web_search_mode="off",
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert not any("intended for the OpenAI Responses API" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_marks_cache_policy_disabled_when_cache_mode_off() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="openai_responses",
        )
    )
    cfg.prompt_cache_mode = "off"

    diagnostics = build_provider_diagnostics(cfg)
    rows = dict(diagnostics.rows())

    assert rows["cache_status"] == "disabled"
    assert rows["cache_strategy"] == "openai_prompt_cache"
    assert "prompt_cache_key" in rows["cache_allowed_fields"]
    assert rows["cache_emitted_fields"] == "none"


def test_provider_diagnostics_allows_openai_responses_streaming() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="openai_responses",
        ),
        stream=True,
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.streaming_supported is True
    assert diagnostics.stream_enabled is True
    assert not any("does not support streaming yet" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_allows_anthropic_native_streaming() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic-native",
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
        ),
        stream=True,
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.streaming_supported is True
    assert diagnostics.stream_enabled is True
    assert not any("does not support streaming yet" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_allows_gemini_native_streaming() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini-native",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-3-flash-preview",
            web_search_adapter="gemini_grounding",
        ),
        stream=True,
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.streaming_supported is True
    assert diagnostics.stream_enabled is True
    assert not any("does not support streaming yet" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_marks_gemini_interactions_experimental(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALYSIS_EXPERIMENTAL_GEMINI_INTERACTIONS", raising=False)
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini-interactions",
            protocol="gemini_interactions",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-2.5-flash",
            web_search_adapter="gemini_grounding",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.protocol_kind == "native"
    assert diagnostics.streaming_supported is False
    assert any("experimental and disabled by default" in issue for issue in diagnostics.issues)
    assert any(
        "Gemini GenerateContent remains the stable native Gemini protocol" in quirk
        for quirk in diagnostics.quirks
    )


def test_provider_diagnostics_allows_enabled_gemini_interactions_experiment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_EXPERIMENTAL_GEMINI_INTERACTIONS", "1")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini-interactions",
            protocol="gemini_interactions",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-2.5-flash",
            web_search_adapter="gemini_grounding",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert not any("experimental and disabled by default" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_reports_external_search_missing_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol="openai_compat",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="tavily",
        ),
        web_search_mode="external",
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.web_search_mode == "external"
    assert diagnostics.web_search_backend_kind == "external"
    assert diagnostics.web_search_registration_ready is False
    assert any("TAVILY_API_KEY" in issue for issue in diagnostics.issues)
    assert any("alysis config set web_search_mode auto" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_reports_policy_disabled_registration(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="openai_responses",
        )
    )
    cfg.web_search_policy = "off"

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.web_search_policy == "off"
    assert diagnostics.web_search_registration_ready is False
    assert any("prevents web_search tool registration" in note for note in diagnostics.notes)


def test_provider_diagnostics_reports_missing_active_profile_api_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic-native",
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.api_key_present is False
    assert diagnostics.api_key_source == "missing"
    assert any(
        "API key is missing" in issue
        and "ANTHROPIC_API_KEY" in issue
        and "alysis config set-api-key" in issue
        for issue in diagnostics.issues
    )


def test_provider_diagnostics_reports_custom_compat_native_search_mismatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="custom",
            protocol="openai_compat",
            base_url="https://gateway.example.test/v1",
            default_model="gateway-model",
            web_search_adapter="auto",
        ),
        web_search_mode="native",
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.protocol_kind == "compatibility"
    assert diagnostics.web_search_registration_ready is False
    assert any("custom OpenAI-compatible profiles" in issue for issue in diagnostics.issues)
    assert any("web_search_mode=native" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_reports_web_search_mode_adapter_mismatch_suggestions() -> None:
    native_mode_cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol="openai_compat",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="tavily",
        ),
        web_search_mode="native",
    )
    external_mode_cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="openai_responses",
        ),
        web_search_mode="external",
    )

    native_issues = build_provider_diagnostics(native_mode_cfg).issues
    external_issues = build_provider_diagnostics(external_mode_cfg).issues

    assert any("web_search_mode=native" in issue for issue in native_issues)
    assert any("alysis config set web_search_mode external" in issue for issue in native_issues)
    assert any("web_search_mode=external" in issue for issue in external_issues)
    assert any("alysis config set web_search_mode native" in issue for issue in external_issues)


def test_provider_diagnostics_reports_native_named_profile_using_compat_protocol() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic-native",
            protocol="openai_compat",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any(
        "named like a native profile" in issue
        and "alysis profile convert anthropic-native --to native" in issue
        for issue in diagnostics.issues
    )


def test_provider_diagnostics_reports_anthropic_first_party_host_using_compat_protocol() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol="openai_compat",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any("legacy compatibility semantics" in issue for issue in diagnostics.issues)
    assert any(
        "Anthropic first-party API using compatibility mode" in issue
        and "alysis profile convert anthropic --to native" in issue
        for issue in diagnostics.issues
    )


def test_provider_diagnostics_does_not_warn_for_explicit_anthropic_compat_profile() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic-compat",
            protocol="openai_compat",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert not any("legacy compatibility semantics" in issue for issue in diagnostics.issues)
    assert not any(
        "Anthropic first-party API using compatibility mode" in issue
        for issue in diagnostics.issues
    )


def test_provider_diagnostics_reports_legacy_gemini_profile_using_compat_protocol() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol="openai_compat",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-3-flash-preview",
            web_search_adapter="gemini_grounding",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any(
        "legacy compatibility semantics" in issue
        and "alysis profile convert gemini --to native" in issue
        for issue in diagnostics.issues
    )


def test_provider_diagnostics_reports_gemini_native_with_openai_compatible_base_url() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-3-flash-preview",
            web_search_adapter="gemini_grounding",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any(
        "Gemini OpenAI-compatible endpoint" in issue
        and "alysis profile convert gemini --to compatibility" in issue
        for issue in diagnostics.issues
    )


def test_provider_diagnostics_reports_openai_responses_with_incompatible_base_url() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://gateway.example.test/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.5",
            web_search_adapter="openai_responses",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any(
        "protocol=openai_responses" in issue
        and "alysis profile convert openai-responses --to compatibility" in issue
        for issue in diagnostics.issues
    )


def test_provider_diagnostics_reports_empty_model() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="",
            web_search_adapter="openai_responses",
        )
    )
    cfg.model = ""

    diagnostics = build_provider_diagnostics(cfg)

    assert diagnostics.model == ""
    assert any("Model is empty" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_reports_model_family_mismatch() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="gemini-2.5-flash",
            web_search_adapter="anthropic_messages",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any("looks like a gemini model" in issue for issue in diagnostics.issues)
    assert any("profile/protocol is for anthropic" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_reports_stale_model_alias() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-2.0-flash",
            web_search_adapter="gemini_grounding",
        )
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any("known renamed/deprecated alias" in issue for issue in diagnostics.issues)
    assert any("gemini-3.6-flash" in issue for issue in diagnostics.issues)


def test_provider_diagnostics_reports_native_feature_model_risk() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-2.5-flash-live-preview-native-audio",
            web_search_adapter="gemini_grounding",
        ),
        stream=True,
        web_search_mode="native",
    )

    diagnostics = build_provider_diagnostics(cfg)

    assert any("Native web_search is enabled" in issue for issue in diagnostics.issues)
    assert any("stream=true is enabled" in issue for issue in diagnostics.issues)


class _FakeClient:
    def __init__(self, response: LLMResponse | Exception) -> None:
        self._response = response

    def chat(self, **_kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _CapturingClient(_FakeClient):
    supports_tool_calling = True

    def __init__(self, response: LLMResponse | Exception) -> None:
        super().__init__(response)
        self.chat_kwargs: dict[str, object] = {}

    def chat(self, **kwargs):
        self.chat_kwargs = dict(kwargs)
        return super().chat(**kwargs)


def test_live_provider_validation_uses_mocked_client_without_printing_secret(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.4-mini",
            web_search_adapter="openai_responses",
        )
    )
    captured: dict[str, object] = {}
    client = _CapturingClient(LLMResponse(content="ok", tool_calls=[], raw={}))

    def factory(**kwargs):
        captured.update(kwargs)
        return client

    validation = validate_active_provider_live(cfg, client_factory=factory)

    assert validation.ok is True
    assert validation.model == "gpt-5.4-mini"
    assert validation.status == "passed"
    assert "sk-openai-secret-value" not in validation.message
    assert captured["api_key"] == "sk-openai-secret-value"
    assert captured["timeout_s"] == 15.0
    assert client.chat_kwargs["tools"]


def test_live_provider_validation_fails_when_tool_probe_is_rejected(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "secret-value")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="tool-probe",
            base_url="https://gateway.example.test/v1",
            api_key_env="ALYSIS_API_KEY",
            default_model="test-model",
            web_search_adapter="auto",
        )
    )
    response = LLMResponse(
        content="ok",
        tool_calls=[],
        raw={},
        provider_metadata={
            "transport": {
                "tools_omitted": True,
                "tools_omit_reason": "provider_rejected_tool_calling",
                "tools_retry_used": True,
            }
        },
    )

    validation = validate_active_provider_live(
        cfg,
        client_factory=lambda **_kwargs: _CapturingClient(response),
    )

    assert validation.status == "failed"
    assert "rejected tool calling" in validation.message
    assert "secret-value" not in validation.message


def test_live_provider_validation_classifies_model_availability_errors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-key")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-bad",
            web_search_adapter="gemini_grounding",
        )
    )

    validation = validate_active_provider_live(
        cfg,
        client_factory=lambda **_kwargs: _FakeClient(RuntimeError("404 model not found")),
    )

    assert validation.status == "failed"
    assert "could not use model 'gemini-bad'" in validation.message
    assert "gemini-secret-key" not in validation.message


def test_doctor_providers_cli_uses_redacted_diagnostics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol="gemini_generate_content",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
            default_model="gemini-3-flash-preview",
            web_search_adapter="gemini_grounding",
        ),
        stream=True,
        web_search_mode="native",
    )
    save_config(cfg)

    result = CliRunner().invoke(
        alysis_app,
        ["doctor", "providers"],
        env={**_env(tmp_path), "GEMINI_API_KEY": "gemini-secret-key"},
    )

    assert result.exit_code == 0
    assert "alysis doctor providers" in result.output
    assert "gemini_generate_content" in result.output
    assert "generativelanguage.googleapis.com" in result.output
    assert "gemini-secret-key" not in result.output
    assert "env:GEMINI_API_KEY (redacted)" in result.output
    assert "stream_enabled" in result.output
    assert "streaming_supported" in result.output
    assert "stream=true" not in result.output


def test_doctor_providers_live_requires_confirmation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.4-mini",
            web_search_adapter="openai_responses",
        )
    )
    save_config(cfg)
    called = False

    def fake_validate(*_args, **_kwargs):
        nonlocal called
        called = True
        return ProviderLiveValidation(
            profile_name="openai-responses",
            provider_key="openai",
            protocol="openai_responses",
            model="gpt-5.4-mini",
            status="passed",
            message="ok",
        )

    monkeypatch.setattr(root_mod, "validate_active_provider_live", fake_validate)

    result = CliRunner().invoke(
        alysis_app,
        ["doctor", "providers", "--live"],
        input="n\n",
        env={**_env(tmp_path), "OPENAI_API_KEY": "sk-openai-secret-value"},
    )

    assert result.exit_code == 0
    assert "may incur provider cost" in result.output
    assert "cancelled" in result.output
    assert "sk-openai-secret-value" not in result.output
    assert called is False


def test_doctor_providers_live_yes_uses_redacted_validation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.4-mini",
            web_search_adapter="openai_responses",
        )
    )
    save_config(cfg)

    def fake_validate(*_args, **_kwargs):
        return ProviderLiveValidation(
            profile_name="openai-responses",
            provider_key="openai",
            protocol="openai_responses",
            model="gpt-5.4-mini",
            status="passed",
            message="Minimal text request completed successfully.",
        )

    def fake_search_validate(*_args, **_kwargs):
        return WebSearchLiveValidation(
            mode="auto",
            configured_adapter="openai_responses",
            resolved_adapter="openai_responses",
            backend_used="ddgs",
            fallback_used=True,
            status="passed",
            message="openai_responses failed (Responses error 400); fallback ddgs served the probe.",
        )

    def fake_reasoning_probe(*_args, **_kwargs):
        return ReasoningSuppressionReport(
            profile_name="openai-responses",
            provider_key="openai",
            protocol="openai_responses",
            model="gpt-5.4-mini",
            outcome="suppressed",
            reasoning_tokens=0,
            message="Provider honored enable_thinking=false (0 reasoning tokens).",
        )

    monkeypatch.setattr(root_mod, "validate_active_provider_live", fake_validate)
    monkeypatch.setattr(root_mod, "validate_web_search_live", fake_search_validate)
    monkeypatch.setattr(root_mod, "probe_reasoning_suppression_live", fake_reasoning_probe)

    result = CliRunner().invoke(
        alysis_app,
        ["doctor", "providers", "--live", "--yes"],
        env={**_env(tmp_path), "OPENAI_API_KEY": "sk-openai-secret-value"},
    )

    assert result.exit_code == 0
    assert "alysis doctor providers --live" in result.output
    assert "gpt-5.4-mini" in result.output
    assert "passed" in result.output
    assert "sk-openai-secret-value" not in result.output
    # The live path also exercises the enabled optional tools end-to-end and
    # reports whether reasoning-off requests are honored.
    assert "web_search live check" in result.output
    assert "fallback_used" in result.output
    assert "reasoning-off live check" in result.output
    assert "reasoning_off_outcome" in result.output


class _CapturingSearchProbe:
    """search_fn stub recording every probe call it receives."""

    def __init__(self, outcomes: list[dict | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _web_search_ready_cfg(*, mode: str = "auto") -> AppConfig:
    return _cfg_with_profile(
        ProfileSpec(
            name="openai-responses",
            protocol="openai_responses",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-5.4-mini",
            web_search_adapter="auto",
        ),
        web_search_mode=mode,
    )


def test_web_search_live_probe_passes_through_resolved_adapter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg()
    probe = _CapturingSearchProbe(
        [
            {
                "backend_adapter": "openai_responses",
                "source_count": 1,
                "sources": [{"url": "https://example.com", "title": "Example"}],
            }
        ]
    )

    validation = validate_web_search_live(cfg, timeout_s=7.0, search_fn=probe)

    assert validation.status == "passed"
    assert validation.ok is True
    assert validation.resolved_adapter == "openai_responses"
    assert validation.backend_used == "openai_responses"
    assert validation.fallback_used is False
    assert len(probe.calls) == 1
    call = probe.calls[0]
    assert call["max_sources"] == 1
    probe_cfg = call["cfg"]
    # The first attempt pins the resolved adapter so its own failure would be
    # reported instead of silently masked by an in-call fallback, and the probe
    # runs on a short timeout so doctor stays cheap.
    assert probe_cfg.web_search_adapter == "openai_responses"
    assert probe_cfg.web_search_timeout_s == 7.0
    assert "sk-openai-secret-value" not in validation.message


def test_web_search_live_probe_reports_native_failure_and_fallback_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A gateway that statically looks ready but rejects real search calls must
    show up as a native failure served by a fallback, not as plain "ready"."""

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg()
    # Static readiness still says the native adapter is fine.
    assert build_provider_diagnostics(cfg).web_search_registration_ready is True

    from alysis_code.tools.web_search import WebSearchError

    probe = _CapturingSearchProbe(
        [
            WebSearchError(
                "Responses error 400: include value 'web_search_call.action.sources' "
                "cannot be produced by this gateway"
            ),
            {
                "backend_adapter": "ddgs",
                "source_count": 1,
                "sources": [{"url": "https://example.com", "title": "Example"}],
            },
        ]
    )

    validation = validate_web_search_live(cfg, search_fn=probe)

    assert validation.status == "passed"
    assert validation.resolved_adapter == "openai_responses"
    assert validation.backend_used == "ddgs"
    assert validation.fallback_used is True
    assert "openai_responses failed" in validation.message
    assert "cannot be produced by this gateway" in validation.message
    assert "fallback ddgs served the probe" in validation.message
    assert len(probe.calls) == 2
    assert probe.calls[0]["cfg"].web_search_adapter == "openai_responses"
    assert probe.calls[1]["cfg"].web_search_adapter == "auto"


def test_web_search_live_probe_reports_failure_when_all_backends_fail(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg()

    from alysis_code.tools.web_search import WebSearchError

    probe = _CapturingSearchProbe(
        [
            WebSearchError("Responses error 400: gateway broken"),
            WebSearchError("web_search failed across auto backends: everything down"),
        ]
    )

    validation = validate_web_search_live(cfg, search_fn=probe)

    assert validation.status == "failed"
    assert validation.ok is False
    assert "gateway broken" in validation.message
    assert "everything down" in validation.message


def test_web_search_live_probe_native_mode_reports_the_native_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg(mode="native")

    from alysis_code.tools.web_search import WebSearchError

    probe = _CapturingSearchProbe(
        [WebSearchError("native web search via openai_responses failed: gateway broken")]
    )

    validation = validate_web_search_live(cfg, search_fn=probe)

    assert validation.status == "failed"
    assert "gateway broken" in validation.message
    # Native mode never tries external backends, so the probe must not either.
    assert len(probe.calls) == 1


def test_web_search_live_probe_env_override_pins_the_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """An explicit env adapter override pins the backend exactly like the real
    web_search call path, so a failed probe must not try fallback backends."""

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_ADAPTER", "openai_responses")
    cfg = _web_search_ready_cfg()

    from alysis_code.tools.web_search import WebSearchError

    probe = _CapturingSearchProbe([WebSearchError("Responses error 400: gateway broken")])

    validation = validate_web_search_live(cfg, search_fn=probe)

    assert validation.status == "failed"
    assert "gateway broken" in validation.message
    assert len(probe.calls) == 1


def test_web_search_live_probe_detects_in_call_fallback_on_first_attempt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """If the pinned attempt is served by a different backend (e.g. an env
    override defeated the pin and an in-call fallback engaged), the report says
    so instead of pretending the resolved adapter worked."""

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg()
    probe = _CapturingSearchProbe(
        [
            {
                "backend_adapter": "ddgs",
                "source_count": 1,
                "sources": [{"url": "https://example.com", "title": "Example"}],
            }
        ]
    )

    validation = validate_web_search_live(cfg, search_fn=probe)

    assert validation.status == "passed"
    assert validation.resolved_adapter == "openai_responses"
    assert validation.backend_used == "ddgs"
    assert validation.fallback_used is True
    assert "did not serve the probe" in validation.message


def test_web_search_live_probe_skips_when_web_search_is_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg(mode="off")

    def _never_called(**_kwargs):
        raise AssertionError("disabled web_search must not be probed")

    validation = validate_web_search_live(cfg, search_fn=_never_called)

    assert validation.status == "skipped"
    assert validation.backend_used == ""


def test_reasoning_suppression_probe_reports_ignored_flag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg()
    captured: dict[str, object] = {}
    response = LLMResponse(
        content="ok",
        tool_calls=[],
        raw={},
        usage=LLMUsage(
            prompt_tokens=10,
            completion_tokens=1290,
            total_tokens=1300,
            reasoning_tokens=1280,
        ),
    )

    def factory(**kwargs):
        captured.update(kwargs)
        return _CapturingClient(response)

    report = probe_reasoning_suppression_live(cfg, client_factory=factory)

    # The probe is built the same way latency-sensitive internal clients are.
    assert captured["enable_thinking"] is False
    assert captured["reasoning_effort"] == ""
    assert report.outcome == "ignored"
    assert report.reasoning_tokens == 1280
    assert "despite enable_thinking=false" in report.message
    assert "sk-openai-secret-value" not in report.message


def test_reasoning_suppression_probe_reports_suppressed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg()
    response = LLMResponse(
        content="ok",
        tool_calls=[],
        raw={},
        usage=LLMUsage(
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
            reasoning_tokens=0,
        ),
    )

    report = probe_reasoning_suppression_live(
        cfg,
        client_factory=lambda **_kwargs: _CapturingClient(response),
    )

    assert report.outcome == "suppressed"
    assert report.reasoning_tokens == 0


def test_reasoning_suppression_probe_reports_not_reported(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    cfg = _web_search_ready_cfg()
    response = LLMResponse(
        content="ok",
        tool_calls=[],
        raw={},
        usage=LLMUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    )

    report = probe_reasoning_suppression_live(
        cfg,
        client_factory=lambda **_kwargs: _CapturingClient(response),
    )

    assert report.outcome == "not_reported"
    assert report.reasoning_tokens is None
    assert "cannot be verified" in report.message


def test_reasoning_suppression_probe_uses_profile_default_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The probe targets the active profile's default model; the legacy
    ALYSIS_MODEL_ROUTER override no longer influences it."""
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-value")
    monkeypatch.setenv("ALYSIS_MODEL_ROUTER", "legacy-router-model")
    cfg = _web_search_ready_cfg()
    captured: dict[str, object] = {}
    response = LLMResponse(
        content="ok",
        tool_calls=[],
        raw={},
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2, reasoning_tokens=0),
    )

    def factory(**kwargs):
        captured.update(kwargs)
        return _CapturingClient(response)

    report = probe_reasoning_suppression_live(cfg, client_factory=factory)

    assert captured["model"] == "gpt-5.4-mini"
    assert report.model == "gpt-5.4-mini"
