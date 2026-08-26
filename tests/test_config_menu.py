from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.cli_impl import config_menu as config_menu_mod
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.cli_impl.config_menu import (
    ConfigMenuState,
    thinking_label_from_cfg,
)
from alysis_code.config import (
    AppConfig,
    ConfigError,
    load_config,
    load_persisted_profile_keys,
    save_config,
    save_persisted_profile_key,
)
from alysis_code.llm.cache_policy import build_prompt_cache_namespace
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
)
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    get_active_profile,
    set_active_profile,
)


def test_state_tracks_dirty_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    state = ConfigMenuState.from_cfg(AppConfig(model="old"))

    state.set_field("model", "new")
    assert state.dirty is True

    state.reset()
    assert state.dirty is False


def test_subscription_execution_uses_native_profile_and_preserves_api_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code import provider_auth as provider_auth_mod

    cfg = AppConfig(model="native-model", base_url="https://api.openai.com/v1")
    cfg.extra_fields = {
        "profiles": {
            "openai": ProfileSpec(
                name="openai",
                base_url="https://api.openai.com/v1",
                default_model="native-model",
            ).to_dict()
        },
        "active_profile": "openai",
    }
    state = ConfigMenuState.from_cfg(cfg)

    state.set_execution_backend("delegated", runtime="openai-codex")
    monkeypatch.setattr(
        provider_auth_mod,
        "create_provider_auth",
        lambda _provider_id: SimpleNamespace(
            account_status=lambda: SimpleNamespace(connected=False)
        ),
    )

    assert state.execution_backend == "delegated"
    assert state.execution_runtime == "openai-codex"
    assert state.agent_runtimes == {}
    assert state.active_profile == "chatgpt-codex"
    assert (
        ProfileSpec.from_dict("chatgpt-codex", state.profiles["chatgpt-codex"]).auth_provider
        == "openai-codex"
    )
    assert state.validate() is None
    assert state.dirty is True

    resolved = state._resolution_cfg()
    assert resolved.execution.backend == "native"
    assert resolved.execution.runtime is None
    assert resolved.agent_runtimes == {}

    target = cfg.model_copy(deep=True)
    result = state.commit_to(target)

    assert result.saved is True
    assert result.changes["active_profile"] == "chatgpt-codex"
    assert result.changes["profiles"] == ["chatgpt-codex", "openai"]
    assert target.execution.backend == "native"
    assert target.execution.runtime is None
    assert target.model == ""
    assert target.base_url == "https://chatgpt.com/backend-api/codex"
    assert target.extra_fields["active_profile"] == "chatgpt-codex"
    assert target.extra_fields["subscription_model_selection_required"] == "openai-codex"

    state.reset()
    assert state.execution_backend == "native"
    assert state.execution_runtime == ""
    assert state.agent_runtimes == {}
    assert state.dirty is False


def test_reselecting_subscription_preserves_config_selected_model_and_effort() -> None:
    cfg = AppConfig(model="kept-model", llm_reasoning_effort="high")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="kept-model",
        reasoning_effort="high",
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    state = ConfigMenuState.from_cfg(cfg)

    state.set_execution_backend("delegated", runtime="openai-codex")

    preserved = ProfileSpec.from_dict(profile.name, state.profiles[profile.name])
    assert preserved.default_model == "kept-model"
    assert preserved.reasoning_effort == "high"
    assert state.fields["model"] == "kept-model"
    assert state.thinking_label == "high"
    assert state.subscription_selection_required is False


def test_subscription_profile_edit_rejects_provider_managed_model_and_endpoint() -> None:
    cfg = AppConfig(model="kept-model", llm_reasoning_effort="high")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="kept-model",
        reasoning_effort="high",
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    state = ConfigMenuState.from_cfg(cfg)

    with pytest.raises(ConfigError, match="provider-managed"):
        state.update_active_profile_spec(
            base_url="https://example.com/v1",
            default_model="not-in-catalog",
        )


def test_profile_edit_preserves_explicit_reasoning_trace_adapter() -> None:
    profile = ProfileSpec(
        name="openrouter",
        protocol="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        default_model="openai/gpt-5",
        reasoning_trace_adapter="openrouter_reasoning",
    )
    cfg = AppConfig(model=profile.default_model)
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    state = ConfigMenuState.from_cfg(cfg)

    state.update_active_profile_spec(notes="edited without touching trace settings")

    edited = ProfileSpec.from_dict(profile.name, state.profiles[profile.name])
    assert edited.reasoning_trace_adapter == "openrouter_reasoning"


def test_subscription_profile_cannot_be_replaced_by_generic_config_add() -> None:
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="kept-model",
        reasoning_effort="high",
    )
    cfg = AppConfig(model=profile.default_model)
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    state = ConfigMenuState.from_cfg(cfg)

    with pytest.raises(ConfigError, match="cannot be overwritten through generic profile"):
        state.add_profile_spec(
            ProfileSpec(
                name=profile.name,
                base_url="https://example.com/v1",
            )
        )

    assert (
        ProfileSpec.from_dict(
            profile.name,
            state.profiles[profile.name],
        )
        == profile
    )


def test_delegated_execution_requires_a_known_configured_runtime() -> None:
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    state.execution_backend = "delegated"

    assert state.validate() == "Choose a supported AI subscription connection."

    state.execution_runtime = "unknown-runtime"
    assert state.validate() == "Unknown AI subscription connection: unknown-runtime"

    state.execution_runtime = "openai-codex"
    assert state.validate() == "AI subscription profile does not match the selected connection."


def test_classic_execution_section_uses_registry_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    choices = iter(("delegated", "openai-codex"))
    picker_calls: list[dict[str, object]] = []

    def _pick(**kwargs: object) -> str:
        picker_calls.append(kwargs)
        return next(choices)

    monkeypatch.setattr(config_menu_mod, "_run_config_picker", _pick)
    managed_accounts: list[str] = []
    monkeypatch.setattr(
        config_menu_mod,
        "_run_subscription_account_section",
        lambda provider_id, _console: managed_accounts.append(provider_id),
    )
    output: list[str] = []
    console = SimpleNamespace(print=lambda value="": output.append(str(value)))

    config_menu_mod._run_execution_section(state, console)

    assert state.execution_backend == "delegated"
    assert state.execution_runtime == "openai-codex"
    assert state.agent_runtimes == {}
    assert state.active_profile == "chatgpt-codex"
    assert picker_calls[0]["title"] == "Model Access"
    access_rows = picker_calls[0]["rows"]
    assert isinstance(access_rows, list)
    assert [row[1] for row in access_rows] == ["Use an API key", "Use an AI subscription"]
    assert all("Codex" not in row[1] for row in access_rows)
    assert picker_calls[1]["title"] == "AI Subscription"
    runtime_rows = picker_calls[1]["rows"]
    assert isinstance(runtime_rows, list)
    assert "openai-codex" in [row[0] for row in runtime_rows]
    assert "AI subscription via ChatGPT Codex subscription" in output[-1]
    assert managed_accounts == ["openai-codex"]


def test_classic_subscription_back_returns_to_model_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    choices = iter(("delegated", None, "native"))
    picker_titles: list[str] = []

    def _pick(**kwargs: object) -> str | None:
        picker_titles.append(str(kwargs["title"]))
        return next(choices)

    monkeypatch.setattr(config_menu_mod, "_run_config_picker", _pick)
    output: list[str] = []
    console = SimpleNamespace(print=lambda value="": output.append(str(value)))

    config_menu_mod._run_execution_section(state, console)

    assert picker_titles == ["Model Access", "AI Subscription", "Model Access"]
    assert state.execution_backend == "native"
    assert output[-1] == "[green]Model access:[/green] API key. Save to apply."


def test_commit_persists_model_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    cfg.model = "old"
    save_config(cfg)

    state = ConfigMenuState.from_cfg(load_config())
    state.set_field("model", "new")
    cfg_to_save = load_config()
    result = state.commit_to(cfg_to_save)
    save_config(cfg_to_save)

    assert result.saved is True
    assert load_config().model == "new"
    assert get_active_profile(load_config()).default_model == "new"


def test_commit_does_not_rewrite_unchanged_subscription_model() -> None:
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    cfg = AppConfig(model="gpt-5.6-sol", max_steps=25)
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("max_steps", "26")

    result = state.commit_to(cfg)

    assert result.changes == {"max_steps": 26}
    assert cfg.model == "gpt-5.6-sol"
    assert get_active_profile(cfg).default_model == "gpt-5.6-luna"


def test_config_menu_round_trips_subagent_timeout() -> None:
    cfg = AppConfig(model="default", subagent_timeout_s=123.5)
    state = ConfigMenuState.from_cfg(cfg)

    assert state.fields["subagent_timeout_s"] == "123.5"
    state.set_subagent_timeout_s("321.5")
    assert state.validate() is None

    result = state.commit_to(cfg)

    assert result.changes["subagent_timeout_s"] == 321.5
    assert cfg.subagent_timeout_s == 321.5


@pytest.mark.parametrize("value", ["", "0", "-1", "nan", "inf", "not-a-number"])
def test_config_menu_rejects_invalid_subagent_timeout(value: str) -> None:
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))

    state.set_field("subagent_timeout_s", value)

    assert state.validate() == "Subagent timeout (seconds) must be a positive number."


def test_commit_persists_default_model_section_to_active_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="old")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            default_model="old",
        ),
    )
    set_active_profile(cfg, "anthropic")
    save_config(cfg)

    state = ConfigMenuState.from_cfg(load_config())
    state.set_field("model", "claude-sonnet-5")
    state.set_field("base_url", "https://anthropic.example/v1")
    cfg_to_save = load_config()
    result = state.commit_to(cfg_to_save)
    save_config(cfg_to_save)

    loaded = load_config()
    profile = get_active_profile(loaded)
    assert result.saved is True
    assert loaded.model == "claude-sonnet-5"
    assert loaded.base_url == "https://anthropic.example/v1"
    assert profile.default_model == "claude-sonnet-5"
    assert profile.base_url == "https://anthropic.example/v1"


def test_commit_persists_prompt_cache_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="default")
    save_config(cfg)

    state = ConfigMenuState.from_cfg(load_config())
    state.set_field("prompt_cache_mode", "auto")
    state.set_field("prompt_cache_key", "repo-main")
    state.set_field("prompt_cache_retention", "24h")
    state.set_field("anthropic_prompt_cache_enabled", "true")
    state.set_field("anthropic_prompt_cache_ttl", "1h")
    state.set_field("cache_aware_compaction", "false")
    state.set_field("cache_aware_min_trigger_ratio", "0.78")
    cfg_to_save = load_config()
    result = state.commit_to(cfg_to_save)
    save_config(cfg_to_save)

    loaded = load_config()
    assert result.saved is True
    assert loaded.prompt_cache_mode == "auto"
    assert loaded.prompt_cache_key == "repo-main"
    assert loaded.prompt_cache_retention == "24h"
    assert loaded.anthropic_prompt_cache_enabled is True
    assert loaded.anthropic_prompt_cache_ttl == "1h"
    assert loaded.extra_fields["compaction"]["cache_aware_compaction"] is False
    assert loaded.extra_fields["compaction"]["cache_aware_min_trigger_ratio"] == 0.78
    assert result.changes["prompt_cache_mode"] == "auto"
    assert result.changes["compaction.cache_aware_compaction"] is False
    assert result.changes["compaction.cache_aware_min_trigger_ratio"] == 0.78


def test_commit_persists_web_search_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="default")
    save_config(cfg)

    state = ConfigMenuState.from_cfg(load_config())
    state.set_field("web_search_policy", "off")
    state.set_field("web_search_mode", "external")
    state.set_field("web_search_adapter", "tavily")
    state.set_field("web_search_base_url", "https://search.example.com/v1")
    state.set_field("web_search_model", "search-model")
    cfg_to_save = load_config()
    result = state.commit_to(cfg_to_save)
    save_config(cfg_to_save)

    loaded = load_config()
    assert result.saved is True
    assert loaded.web_search_policy == "off"
    assert loaded.web_search_mode == "external"
    assert loaded.web_search_adapter == "tavily"
    assert loaded.web_search_base_url == "https://search.example.com/v1"
    assert loaded.web_search_model == "search-model"
    assert result.changes["web_search_policy"] == "off"
    assert result.changes["web_search_mode"] == "external"


def test_commit_persists_subagent_role_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    state.set_role_model("coding", "anthropic/claude-sonnet-4-6")
    cfg_to_save = load_config()

    result = state.commit_to(cfg_to_save)
    save_config(cfg_to_save)

    assert result.saved is True
    assert load_config().extra_fields["role_models"]["coding"] == "anthropic/claude-sonnet-4-6"


def test_commit_clears_role_override_when_emptied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    cfg.extra_fields["role_models"] = {"coding": "x"}
    save_config(cfg)

    state = ConfigMenuState.from_cfg(load_config())
    state.set_role_model("coding", "")
    cfg_to_save = load_config()
    result = state.commit_to(cfg_to_save)
    save_config(cfg_to_save)

    assert result.saved is True
    assert "coding" not in load_config().extra_fields.get("role_models", {})


def test_commit_persists_forge_role_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    state.set_forge_role_model("planner", "anthropic/claude-opus-4-7")
    cfg_to_save = load_config()

    result = state.commit_to(cfg_to_save)
    save_config(cfg_to_save)

    assert result.saved is True
    assert load_config().extra_fields["forge_role_models"]["planner"] == "anthropic/claude-opus-4-7"


def test_commit_invalid_timeout_returns_validation_error() -> None:
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    state.set_field("llm_timeout_s", "not-a-number")

    result = state.commit_to(AppConfig(model="default"))

    assert result.saved is False
    assert result.error == "Request timeout (seconds) must be a positive number."


def test_commit_invalid_base_url_returns_validation_error() -> None:
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    state.set_field("base_url", "not-a-url")

    result = state.commit_to(AppConfig(model="default"))

    assert result.saved is False
    assert "Base URL" in str(result.error)


def test_state_from_cfg_tolerates_invalid_reasoning_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", "extreme")

    state = ConfigMenuState.from_cfg(AppConfig(model="default"))

    assert state.thinking_label == "auto"
    assert state.config_warning is not None
    assert "ALYSIS_LLM_REASONING_EFFORT" in state.config_warning


def test_switching_profile_refreshes_api_key_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    cfg = AppConfig(model="gpt-test")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1"))
    set_active_profile(cfg, "openai")
    save_config(cfg)
    save_persisted_profile_key("anthropic", "sk-ant")

    state = ConfigMenuState.from_cfg(load_config())
    state.set_active_profile_name("anthropic")

    assert state.api_key_source == "stored:profile=anthropic"
    assert state.masked_api_key != "(not set)"


def test_default_model_rows_include_active_profile_preset_suggestions() -> None:
    cfg = AppConfig(model="deepseek-v4-pro")
    add_profile(
        cfg,
        ProfileSpec(
            name="deepseek",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
            default_model="deepseek-v4-pro",
        ),
    )
    set_active_profile(cfg, "deepseek")
    state = ConfigMenuState.from_cfg(cfg)

    model_values = [
        value for value, _label, _description in config_menu_mod._default_model_rows(state)
    ]

    assert "deepseek-v4-pro" in model_values
    assert "deepseek-v4-flash" in model_values
    assert "deepseek-coder" not in model_values


def test_nvidia_default_model_rows_merge_live_third_party_catalog_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    preset = get_preset("nvidia")
    assert preset is not None
    cfg = AppConfig(model="nvidia/nemotron-3-super-120b-a12b")
    add_profile(cfg, make_profile_from_preset(preset, name="nvidia"))
    set_active_profile(cfg, "nvidia")
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("new_api_key", "nvapi-test")
    calls: list[str] = []

    def discover(*, profile: ProfileSpec, api_key: str, **_kwargs: object):
        calls.append(api_key)
        assert profile.base_url == "https://integrate.api.nvidia.com/v1"
        return (
            config_menu_mod.ProviderModelOption(
                id="deepseek-ai/deepseek-v4-pro",
                label="deepseek-ai/deepseek-v4-pro",
            ),
            config_menu_mod.ProviderModelOption(
                id="meta/llama-3.3-70b-instruct",
                label="meta/llama-3.3-70b-instruct",
            ),
        )

    monkeypatch.setattr(config_menu_mod, "discover_provider_models", discover)

    first = config_menu_mod._default_model_rows(state)
    second = config_menu_mod._default_model_rows(state)
    model_values = [value for value, _label, _description in first]

    assert calls == ["nvapi-test"]
    assert first == second
    assert model_values.count("deepseek-ai/deepseek-v4-pro") == 1
    assert "meta/llama-3.3-70b-instruct" in model_values
    live_meta_row = next(row for row in first if row[0] == "meta/llama-3.3-70b-instruct")
    assert live_meta_row[1].endswith("(chat compatibility unverified)")
    assert "does not declare endpoint compatibility" in live_meta_row[2]
    assert model_values.index(config_menu_mod._CUSTOM_MODEL_VALUE) < model_values.index(
        "meta/llama-3.3-70b-instruct"
    )
    assert config_menu_mod._thinking_labels_for_state(
        state,
        model="deepseek-ai/deepseek-v4-pro",
    ) == ("off", "high", "max", "auto")
    assert config_menu_mod._thinking_labels_for_state(
        state,
        model="new-vendor/model-released-today",
    ) == ("auto",)


def test_switching_to_nvidia_resets_effort_inherited_from_another_provider() -> None:
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    openai = get_preset("openai")
    nvidia = get_preset("nvidia")
    assert openai is not None
    assert nvidia is not None
    cfg = AppConfig(
        model="gpt-5.4",
        llm_enable_thinking=True,
        llm_reasoning_effort="xhigh",
    )
    cfg.extra_fields["llm_thinking_label"] = "xhigh"
    add_profile(cfg, make_profile_from_preset(openai, name="openai"))
    add_profile(cfg, make_profile_from_preset(nvidia, name="nvidia"))
    set_active_profile(cfg, "openai")
    state = ConfigMenuState.from_cfg(cfg)

    assert state.thinking_label == "xhigh"
    state.set_active_profile_name("nvidia")

    assert state.fields["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert state.thinking_label == "auto"
    assert state.thinking_label_explicitly_set is True
    assert config_menu_mod._thinking_labels_for_state(state) == (
        "off",
        "low",
        "high",
        "auto",
    )


def test_loading_active_nvidia_profile_normalizes_stale_effort_to_auto() -> None:
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    nvidia = get_preset("nvidia")
    assert nvidia is not None
    cfg = AppConfig(
        model="nvidia/nemotron-3-super-120b-a12b",
        llm_enable_thinking=True,
        llm_reasoning_effort="xhigh",
    )
    cfg.extra_fields["llm_thinking_label"] = "xhigh"
    add_profile(cfg, make_profile_from_preset(nvidia, name="nvidia"))
    set_active_profile(cfg, "nvidia")

    state = ConfigMenuState.from_cfg(cfg)

    assert state.thinking_label == "auto"
    assert state.thinking_label_explicitly_set is True


def test_zai_coding_plan_drops_stale_effort_and_never_offers_reasoning_off() -> None:
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    zai = get_preset("zai-coding-plan")
    assert zai is not None
    cfg = AppConfig(
        model="glm-5.3",
        llm_enable_thinking=False,
        llm_reasoning_effort="none",
    )
    cfg.extra_fields["llm_thinking_label"] = "off"
    add_profile(cfg, make_profile_from_preset(zai, name="zai-coding-plan"))
    set_active_profile(cfg, "zai-coding-plan")

    state = ConfigMenuState.from_cfg(cfg)

    assert state.thinking_label == "auto"
    assert state.thinking_label_explicitly_set is True
    assert config_menu_mod._thinking_labels_for_state(state) == (
        "low",
        "high",
        "max",
        "auto",
    )


def test_default_model_rows_include_discovered_alysis_trial_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    # Live gateway advertises a provider-prefixed variant of a curated model,
    # plus a genuinely new one.
    monkeypatch.setattr(
        "alysis_code.account_login.list_trial_models",
        lambda _cfg: ["deepseek/deepseek-v4-flash", "deepseek-v5-preview"],
    )

    cfg = AppConfig(model="deepseek-v4-flash")
    add_profile(cfg, make_profile_from_preset(get_preset("alysis"), name="alysis"))
    set_active_profile(cfg, "alysis")
    state = ConfigMenuState.from_cfg(cfg)

    model_values = [
        value for value, _label, _description in config_menu_mod._default_model_rows(state)
    ]
    # Curated clean names present...
    assert "deepseek-v4-flash" in model_values
    assert "deepseek-v4-pro" in model_values
    # ...the provider-prefixed duplicate of a curated model is suppressed...
    assert "deepseek/deepseek-v4-flash" not in model_values
    # ...but a genuinely new discovered model still shows.
    assert "deepseek-v5-preview" in model_values


def test_default_model_rows_survive_alysis_discovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))

    def _boom(_cfg: object) -> list[str]:
        raise RuntimeError("proxy down")

    monkeypatch.setattr("alysis_code.account_login.list_trial_models", _boom)

    cfg = AppConfig(model="deepseek-v4-flash")
    add_profile(cfg, make_profile_from_preset(get_preset("alysis"), name="alysis"))
    set_active_profile(cfg, "alysis")
    state = ConfigMenuState.from_cfg(cfg)

    # Discovery blew up, but the static preset rows must still render the menu.
    model_values = [
        value for value, _label, _description in config_menu_mod._default_model_rows(state)
    ]
    assert "deepseek-v4-flash" in model_values


def test_default_model_rows_fallback_to_base_url_provider() -> None:
    cfg = AppConfig(model="deepseek-v4-pro")
    add_profile(
        cfg,
        ProfileSpec(
            name="deepseek",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
            default_model="deepseek-v4-pro",
        ),
    )
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-sonnet-5",
        ),
    )
    set_active_profile(cfg, "deepseek")
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("base_url", "https://api.anthropic.com/v1/")

    model_values = [
        value for value, _label, _description in config_menu_mod._default_model_rows(state)
    ]

    assert "claude-sonnet-5" in model_values
    assert "deepseek-v4-flash" not in model_values


def test_config_reload_updates_clients_with_effective_profile_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-test")
    cfg = AppConfig(model="claude-sonnet-4-6", base_url="https://api.openai.com/v1")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            extra_headers={"anthropic-version": "2023-06-01"},
        ),
    )
    set_active_profile(cfg, "anthropic")
    cfg.base_url = "https://api.openai.com/v1"
    client = SimpleNamespace(
        base_url="",
        api_key="",
        model="",
        timeout_s=0.0,
        temperature=0.0,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        enable_thinking=None,
        reasoning_effort=None,
        extra_headers={},
        provider_key=None,
        provider_concurrency_caps={},
        provider_retry_settings=None,
    )
    session = SimpleNamespace(
        cfg=cfg,
        client=client,
        router_client=None,
        conversation_compactor=None,
        mode="review",
    )
    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "_refresh_chat_hud_context_cache",
        lambda _session: None,
        raising=False,
    )

    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)

    assert client.base_url == "https://api.anthropic.com/v1"
    assert client.extra_headers == {"anthropic-version": "2023-06-01"}


def test_config_reload_recomputes_auto_reasoning_trace_capability_for_model_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-test")
    cfg = AppConfig(
        model="deepseek-reasoner",
        base_url="https://gateway.example/v1",
    )
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="gateway",
            base_url="https://gateway.example/v1",
            default_model=cfg.model,
        ),
    )
    set_active_profile(cfg, "gateway")
    client = SimpleNamespace(
        base_url="",
        api_key="",
        model="",
        timeout_s=0.0,
        temperature=0.0,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        enable_thinking=None,
        reasoning_effort=None,
        extra_headers={},
        provider_key=None,
        provider_concurrency_caps={},
        provider_retry_settings=None,
    )
    session = SimpleNamespace(
        cfg=cfg,
        client=client,
        router_client=None,
        conversation_compactor=None,
        mode="review",
    )
    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "_refresh_chat_hud_context_cache",
        lambda _session: None,
        raising=False,
    )

    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
    assert client.reasoning_trace_capability.adapter == "deepseek_reasoning"

    session.cfg.extra_fields["model_metadata_overrides"] = {
        "models": {"deepseek-reasoner": {"supports_reasoning": False}}
    }
    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)
    assert client.reasoning_trace_capability.adapter == "openai_compat_passive"
    assert client.reasoning_trace_capability.model_supports_reasoning is False

    session.cfg.extra_fields.pop("model_metadata_overrides")
    session.cfg.model = "vendor/plain-model"
    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)

    assert client.reasoning_trace_capability.adapter == "openai_compat_passive"


def test_staged_api_key_stays_bound_to_its_profile_across_switch_and_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from alysis_code.profile_presets import get_preset, make_profile_from_preset

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    gemini_preset = get_preset("gemini")
    anthropic_preset = get_preset("anthropic")
    assert gemini_preset is not None and anthropic_preset is not None
    gemini = make_profile_from_preset(gemini_preset, name="gemini")
    anthropic = make_profile_from_preset(anthropic_preset, name="anthropic")
    cfg = AppConfig(model=gemini.default_model, base_url=gemini.base_url)
    add_profile(cfg, gemini)
    add_profile(cfg, anthropic)
    set_active_profile(cfg, gemini.name)
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("new_api_key", "gemini-secret")
    state.set_active_profile_name(anthropic.name)

    result = config_menu_mod._save_and_exit(
        state,
        cfg,
        SimpleNamespace(print=lambda *_args, **_kwargs: None),
    )

    assert result.saved is True
    assert state.staged_api_key_target_profile() == "gemini"
    stored = load_persisted_profile_keys()
    assert stored["gemini"] == "gemini-secret"
    assert "anthropic" not in stored


def test_removing_profile_discards_its_staged_api_key() -> None:
    cfg = AppConfig(model="a")
    cfg.extra_fields = {
        "profiles": {
            "a": ProfileSpec(name="a", base_url="https://a.example/v1").to_dict(),
            "b": ProfileSpec(name="b", base_url="https://b.example/v1").to_dict(),
        },
        "active_profile": "a",
    }
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("new_api_key", "a-secret")

    discarded = state.remove_profile_name("a")

    assert discarded is True
    assert state.new_api_key == ""
    assert state.new_api_key_profile is None


def test_config_reload_updates_gemini_cached_content_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "test-key")
    cfg = AppConfig(
        model="gemini-3-flash-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        prompt_cache_mode="auto",
        prompt_cache_retention="15m",
    )
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            default_model="gemini-3-flash-preview",
        ),
    )
    set_active_profile(cfg, "gemini")
    client = SimpleNamespace(
        base_url="",
        api_key="",
        model="",
        timeout_s=0.0,
        temperature=0.0,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        prompt_cache_policy_metadata=None,
        explicit_cached_content_enabled=False,
        cached_content_ttl="3600s",
        cached_content_min_tokens=None,
        _cached_content_by_signature={"old-signature": object()},
        _cached_content_create_disabled_reason="old-route-rejection",
        _cached_content_create_transient_failures=3,
        enable_thinking=None,
        reasoning_effort=None,
        extra_headers={},
        provider_key=None,
        provider_concurrency_caps={},
        provider_retry_settings=None,
    )
    cache_entries_seen_by_settings_apply: list[dict[str, object]] = []

    def apply_cache_settings(
        *, enabled: bool | None, ttl: str | None, min_tokens: int | None
    ) -> None:
        cache_entries_seen_by_settings_apply.append(dict(client._cached_content_by_signature))
        client.explicit_cached_content_enabled = bool(enabled)
        client.cached_content_ttl = str(ttl or "3600s")
        client.cached_content_min_tokens = min_tokens

    client.apply_cache_settings = apply_cache_settings
    session = SimpleNamespace(
        cfg=AppConfig(),
        client=client,
        router_client=None,
        conversation_compactor=None,
        mode="review",
        root=tmp_path,
    )
    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "_refresh_chat_hud_context_cache",
        lambda _session: None,
        raising=False,
    )

    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)

    assert client.explicit_cached_content_enabled is True
    assert client.cached_content_ttl == "900s"
    assert client._cached_content_by_signature == {}
    assert cache_entries_seen_by_settings_apply == [{}]
    assert client._cached_content_create_disabled_reason is None
    assert client._cached_content_create_transient_failures == 0
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "enabled"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == ["cached_content"]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == ["cached_content"]
    assert client.prompt_cache_policy_metadata["ttl"] == "900s"


@pytest.mark.parametrize(
    ("fixed_step_override", "expected_max_steps"),
    [(None, 73), (19, 19)],
)
def test_config_reload_updates_main_model_and_leaves_router_client_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_step_override: int | None,
    expected_max_steps: int,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-test")
    monkeypatch.delenv("ALYSIS_ROUTING_MODE", raising=False)
    cfg = AppConfig(model="coding-v2", routing_mode="auto", max_steps=73)
    cfg.extra_fields = {"role_models": {"router": "router-v2"}}
    client = SimpleNamespace(
        base_url="",
        api_key="",
        model="coding-v1",
        timeout_s=0.0,
        temperature=0.0,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        enable_thinking=None,
        reasoning_effort=None,
        extra_headers={},
        provider_key=None,
        provider_concurrency_caps={},
        provider_retry_settings=None,
    )
    router_client = SimpleNamespace(
        base_url="",
        api_key="",
        model="router-v1",
        timeout_s=0.0,
        temperature=0.8,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        enable_thinking=True,
        reasoning_effort="high",
        extra_headers={},
        provider_key=None,
        provider_concurrency_caps={},
        provider_retry_settings=None,
    )
    session = SimpleNamespace(
        cfg=AppConfig(model="coding-v1", routing_mode="auto", max_steps=25),
        client=client,
        router_client=router_client,
        conversation_compactor=None,
        mode="review",
        routing_mode="auto",
        max_steps=25,
        chat_turn_fixed_override=fixed_step_override,
        root=tmp_path,
    )
    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "_refresh_chat_hud_context_cache",
        lambda _session: None,
        raising=False,
    )

    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)

    assert client.model == "coding-v2"
    # Router-free path: no router client participates in config reload; an
    # embedder-supplied one is left exactly as it was.
    assert router_client.model == "router-v1"
    assert router_client.temperature == 0.8
    assert router_client.enable_thinking is True
    assert router_client.reasoning_effort == "high"
    assert not hasattr(router_client, "route_identity")
    assert session.routing_mode == "auto"
    assert session.max_steps == expected_max_steps

    first_main_route = client.route_identity
    assert first_main_route.model == "coding-v2"
    assert first_main_route.credential_scope

    monkeypatch.setenv("ALYSIS_API_KEY", "sk-next-credential")
    next_cfg = AppConfig(model="coding-v3", routing_mode="auto", max_steps=73)
    next_cfg.extra_fields = {"role_models": {"router": "router-v3"}}
    add_profile(
        next_cfg,
        ProfileSpec(
            name="alternate-route",
            protocol="openai_compat",
            base_url="https://openrouter.ai/api/v1",
            default_model="coding-v3",
            extra_headers={"OpenAI-Project": "project-b"},
        ),
    )
    set_active_profile(next_cfg, "alternate-route")

    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=next_cfg)

    assert client.route_identity.model == "coding-v3"
    assert not hasattr(router_client, "route_identity")
    assert router_client.model == "router-v1"
    assert client.route_identity.profile_name == "alternate-route"
    assert client.route_identity.base_url == "https://openrouter.ai/api/v1"
    assert client.route_identity.provider_key == "openrouter"
    assert client.route_identity.credential_scope != first_main_route.credential_scope
    assert client.route_identity.routing_headers
    assert client.route_identity.fingerprint != first_main_route.fingerprint


@pytest.mark.parametrize(
    ("protocol", "base_url"),
    [
        (OPENAI_COMPAT_PROTOCOL, "https://gateway.example.test/v1"),
        (OPENAI_COMPAT_PROTOCOL, "https://openrouter.ai/api/v1"),
        (ANTHROPIC_MESSAGES_PROTOCOL, "https://api.anthropic.com/v1"),
    ],
)
def test_config_reload_preserves_factory_route_for_unrelated_changes(
    protocol: str,
    base_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "stable-credential")
    monkeypatch.delenv("ALYSIS_ROUTING_MODE", raising=False)
    profile = ProfileSpec(
        name=f"route-{protocol}",
        protocol=protocol,
        base_url=base_url,
        default_model="route-model",
        extra_headers={"X-Tenant-ID": "tenant-a"},
    )
    cfg = AppConfig(model="route-model", routing_mode="code_only", max_steps=25)
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    save_persisted_profile_key(profile.name, "stable-credential")
    if "openrouter.ai" in base_url:
        cfg.prompt_cache_mode = "auto"
    session_id = "session-route-stability"
    client = make_llm_client(
        cfg=cfg,
        api_key="stable-credential",
        model=cfg.model,
        profile=profile,
        session_id=session_id,
        prompt_cache_namespace=build_prompt_cache_namespace(
            workspace_root=tmp_path,
            role="coding",
            profile_name=profile.name,
        ),
    )
    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        client._thinking_display_supported = False
        client._input_token_count_available = False
        client._temperature_omit_after_rejection = True
    else:
        client._disabled_prompt_cache_fields.add("prompt_cache_key")
        client._temperature_compat_modes[("provider", "model")] = "omit_temperature"
        client._tool_calling_compat_disabled.add(("provider", "model", "base_url"))
    initial_route_identity = client.route_identity
    initial_fingerprint = initial_route_identity.fingerprint
    session = SimpleNamespace(
        cfg=cfg,
        client=client,
        router_client=None,
        conversation_compactor=None,
        mode="review",
        routing_mode="code_only",
        max_steps=25,
        chat_turn_fixed_override=None,
        root=tmp_path,
        store=SimpleNamespace(session_id=session_id),
    )
    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "_refresh_chat_hud_context_cache",
        lambda _session: None,
        raising=False,
    )

    cfg.max_steps = 26
    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)

    assert client.route_identity == initial_route_identity
    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        assert client._thinking_display_supported is False
        assert client._input_token_count_available is False
        assert client._temperature_omit_after_rejection is True
    else:
        assert client._disabled_prompt_cache_fields == {"prompt_cache_key"}
        assert client._temperature_compat_modes
        assert client._tool_calling_compat_disabled

    session.cfg.model = "changed-route-model"
    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)

    assert client.route_identity.fingerprint != initial_fingerprint
    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        assert client._thinking_display_supported is None
        assert client._input_token_count_available is None
        assert client._temperature_omit_after_rejection is False
    else:
        assert client._disabled_prompt_cache_fields == set()
        assert client._temperature_compat_modes == {}
        assert client._tool_calling_compat_disabled == set()


@pytest.mark.parametrize(
    ("field_name", "initial_value", "reset_value"),
    [
        ("_reasoning_summary_supported", False, None),
        ("_thinking_display_supported", False, None),
        ("_thought_summaries_supported", False, None),
        ("_thinking_summaries_supported", False, None),
        ("_input_token_count_available", False, None),
        ("_temperature_omit_after_rejection", True, False),
        ("_cached_content_create_disabled_reason", "rejected", None),
        ("_cached_content_create_transient_failures", 3, 0),
    ],
)
def test_route_local_client_capability_flags_reset_only_when_route_changes(
    field_name: str,
    initial_value: object,
    reset_value: object,
) -> None:
    client = SimpleNamespace(**{field_name: initial_value})

    chat_loop._reset_route_local_client_capabilities(client, route_changed=False)
    assert getattr(client, field_name) == initial_value

    chat_loop._reset_route_local_client_capabilities(client, route_changed=True)
    assert getattr(client, field_name) == reset_value


def test_route_local_client_cached_state_clears_only_when_route_changes() -> None:
    client = SimpleNamespace(
        _cached_content_by_signature={"signature": "cached-content-id"},
        _disabled_prompt_cache_fields={"prompt_cache_key"},
        _disabled_prompt_cache_fields_lock=threading.Lock(),
        _temperature_compat_modes={("provider", "model"): "omit_temperature"},
        _temperature_compat_lock=threading.Lock(),
        _tool_choice_compat_disabled={("provider", "model")},
        _tool_choice_compat_lock=threading.Lock(),
        _tool_calling_compat_disabled={("provider", "model", "url")},
        _tool_calling_compat_lock=threading.Lock(),
    )

    chat_loop._reset_route_local_client_capabilities(client, route_changed=False)
    assert client._cached_content_by_signature
    assert client._disabled_prompt_cache_fields
    assert client._temperature_compat_modes
    assert client._tool_choice_compat_disabled
    assert client._tool_calling_compat_disabled

    chat_loop._reset_route_local_client_capabilities(client, route_changed=True)
    assert client._cached_content_by_signature == {}
    assert client._disabled_prompt_cache_fields == set()
    assert client._temperature_compat_modes == {}
    assert client._tool_choice_compat_disabled == set()
    assert client._tool_calling_compat_disabled == set()


def test_config_reload_failure_restores_session_and_all_client_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code.model_registry import ModelRegistry

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_ROUTING_MODE", raising=False)

    def make_cfg(*, model: str, base_url: str) -> tuple[AppConfig, ProfileSpec]:
        profile = ProfileSpec(
            name="transactional-route",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url=base_url,
            default_model=model,
            extra_headers={"X-Tenant-ID": f"tenant-{model}"},
        )
        config = AppConfig(model=model, routing_mode="auto", max_steps=25)
        config.extra_fields = {
            "profiles": {},
            "active_profile": "",
            "role_models": {
                "router": f"router-{model}",
                "compactor": f"compactor-{model}",
            },
        }
        add_profile(config, profile)
        set_active_profile(config, profile.name)
        return config, profile

    old_cfg, old_profile = make_cfg(
        model="old-model",
        base_url="https://old-gateway.example/v1",
    )
    next_cfg, _next_profile = make_cfg(
        model="new-model",
        base_url="https://new-gateway.example/v1",
    )
    save_persisted_profile_key(old_profile.name, "stable-credential")
    session_id = "transactional-session"

    def make_client(model: str):
        return make_llm_client(
            cfg=old_cfg,
            api_key="stable-credential",
            model=model,
            profile=old_profile,
            session_id=session_id,
        )

    main_client = make_client("old-model")
    router_client = make_client("router-old-model")
    compactor_client = make_client("compactor-old-model")
    clients = [main_client, router_client, compactor_client]
    for client in clients:
        client._disabled_prompt_cache_fields.add("prompt_cache_key")

    def outbound_snapshot(client: object) -> tuple[object, ...]:
        route_client = client  # type: ignore[assignment]
        return (
            route_client.base_url,
            route_client.api_key,
            route_client.model,
            route_client.route_identity,
            dict(route_client.extra_headers),
            set(route_client._disabled_prompt_cache_fields),
        )

    before = [outbound_snapshot(client) for client in clients]
    session = SimpleNamespace(
        cfg=old_cfg,
        client=main_client,
        router_client=router_client,
        conversation_compactor=SimpleNamespace(compactor_client=compactor_client),
        mode="review",
        routing_mode="auto",
        max_steps=25,
        chat_turn_fixed_override=None,
        root=tmp_path,
        store=SimpleNamespace(session_id=session_id),
    )
    original_get = ModelRegistry.get
    get_calls = 0

    def fail_during_router_refresh(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal get_calls
        get_calls += 1
        if get_calls == 2:
            raise RuntimeError("injected route refresh failure")
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(ModelRegistry, "get", fail_during_router_refresh)

    with pytest.raises(RuntimeError, match="injected route refresh failure"):
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=next_cfg)

    assert get_calls == 2
    assert session.cfg is old_cfg
    assert session.routing_mode == "auto"
    assert session.max_steps == 25
    assert not hasattr(session, "api_key")
    assert [outbound_snapshot(client) for client in clients] == before


@pytest.mark.parametrize(
    ("current_mode", "next_mode", "router_client"),
    [
        ("auto", "code_only", object()),
        ("code_only", "auto", None),
    ],
)
def test_config_reload_applies_routing_mode_change_in_place(
    current_mode: str,
    next_mode: str,
    router_client: object | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Router-free path: routing_mode is a deprecated no-op field, so changing
    # it is an ordinary live reload — no restart is required and the session
    # simply tracks the configured value.
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-test")
    monkeypatch.delenv("ALYSIS_ROUTING_MODE", raising=False)
    current_cfg = AppConfig(model="coding-v1", routing_mode=current_mode, max_steps=25)
    next_cfg = AppConfig(model="coding-v2", routing_mode=next_mode, max_steps=73)
    client = SimpleNamespace(
        base_url="",
        api_key="",
        model="coding-v1",
        timeout_s=0.0,
        temperature=0.0,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        enable_thinking=None,
        reasoning_effort=None,
        extra_headers={},
        provider_key=None,
        provider_concurrency_caps={},
        provider_retry_settings=None,
    )
    session = SimpleNamespace(
        cfg=current_cfg,
        client=client,
        router_client=router_client,
        conversation_compactor=None,
        mode="review",
        routing_mode=current_mode,
        max_steps=25,
        chat_turn_fixed_override=None,
        root=tmp_path,
    )
    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "_refresh_chat_hud_context_cache",
        lambda _session: None,
        raising=False,
    )

    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=next_cfg)

    assert session.routing_mode == next_mode
    assert session.cfg.routing_mode == next_mode
    assert session.max_steps == 73
    assert client.model == "coding-v2"
    assert session.router_client is router_client


def test_classic_config_reload_applies_routing_mode_change_without_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Router-free path: a routing-mode-only save reloads live like any other
    # config change instead of closing the chat session.
    current_cfg = AppConfig(model="coding", routing_mode="auto")
    next_cfg = AppConfig(model="coding", routing_mode="code_only")
    session = SimpleNamespace(
        cfg=current_cfg,
        routing_mode="auto",
        router_client=None,
    )
    applied: list[AppConfig] = []
    output: list[str] = []
    console = SimpleNamespace(print=lambda value="": output.append(str(value)))
    monkeypatch.delenv("ALYSIS_ROUTING_MODE", raising=False)
    monkeypatch.setattr(
        config_menu_mod,
        "run_config_menu",
        lambda: SimpleNamespace(
            saved=True,
            changes={"routing_mode": "code_only"},
            api_key_changed=False,
        ),
    )
    monkeypatch.setattr(chat_loop, "load_config", lambda: next_cfg, raising=False)
    monkeypatch.setattr(
        chat_loop,
        "_apply_config_menu_changes_to_session",
        lambda *, session, cfg: applied.append(cfg),
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "_is_non_interactive_terminal",
        lambda: False,
        raising=False,
    )
    monkeypatch.setattr(chat_loop, "_is_forge_ui_mode", lambda _mode: False, raising=False)
    monkeypatch.setattr(
        chat_loop,
        "_parse_forge_enter_command",
        lambda **_kwargs: None,
        raising=False,
    )

    result = chat_loop._handle_chat_command(
        input_text="/config",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=chat_loop._ForgeChatState(),
        plan_mode_state=chat_loop._ChatPlanModeState(),
    )

    rendered = "\n".join(output)
    assert result == "handled"
    assert applied == [next_cfg]
    assert "Routing mode changed" not in rendered
    assert "session is closing" not in rendered
    assert "apply on the next user turn" in rendered


@pytest.mark.parametrize(
    "label",
    ["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "auto"],
)
def test_thinking_label_round_trip(label: str) -> None:
    cfg = AppConfig(model="default")
    state = ConfigMenuState.from_cfg(cfg)
    state.set_thinking_label(label)

    result = state.commit_to(cfg)

    assert result.saved is True
    assert thinking_label_from_cfg(cfg) == label
    if label == "off":
        assert cfg.llm_reasoning_effort == "none"
    elif label == "auto":
        assert cfg.llm_reasoning_effort is None
    else:
        assert cfg.llm_reasoning_effort == label


def test_commit_does_not_persist_env_reasoning_effort_for_unrelated_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", "high")
    cfg = AppConfig(model="old")
    state = ConfigMenuState.from_cfg(cfg)

    state.set_field("model", "new")
    result = state.commit_to(cfg)

    assert result.saved is True
    assert cfg.model == "new"
    assert cfg.llm_reasoning_effort is None
    assert "llm_reasoning_effort" not in result.changes
    assert "llm_enable_thinking" not in result.changes


def test_commit_persists_explicit_env_reasoning_effort_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", "high")
    cfg = AppConfig(model="default")
    state = ConfigMenuState.from_cfg(cfg)

    state.set_thinking_label("high")
    result = state.commit_to(cfg)

    assert result.saved is True
    assert cfg.llm_reasoning_effort == "high"
    assert result.changes["llm_reasoning_effort"] == "high"


def _state_with_active_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigMenuState:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = AppConfig(model="claude-sonnet-4-6")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-sonnet-4-6",
        ),
    )
    set_active_profile(cfg, "anthropic")
    return ConfigMenuState.from_cfg(cfg)


def test_invalid_prompt_cache_mode_env_logs_warning_and_summary_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state_with_active_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_PROMPT_CACHE_MODE", "bogus")

    with caplog.at_level(logging.WARNING, logger="alysis_code.cli_impl.config_menu"):
        policy = config_menu_mod._resolved_cache_policy_for_state(state)
        summary = config_menu_mod._cache_summary_text(state)

    assert policy is None
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "cache policy resolution failed" in record.getMessage().lower()
    ]
    assert warnings
    assert "ALYSIS_PROMPT_CACHE_MODE" in warnings[0].getMessage()
    assert "policy unavailable: ALYSIS_PROMPT_CACHE_MODE must be one of" in summary
    assert "policy unknown" not in summary


def test_cache_policy_summary_without_profile_stays_policy_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    state = ConfigMenuState.from_cfg(AppConfig(model="default"))
    state.active_profile = ""

    summary = config_menu_mod._cache_summary_text(state)

    assert "policy unknown" in summary


def test_effective_cache_capability_failure_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state_with_active_profile(tmp_path, monkeypatch)

    def _boom(profile: ProfileSpec) -> None:
        raise RuntimeError("preset lookup exploded")

    monkeypatch.setattr(config_menu_mod, "find_preset_for_profile", _boom)
    with caplog.at_level(logging.WARNING, logger="alysis_code.cli_impl.config_menu"):
        capability = config_menu_mod._effective_cache_capability_for_state(state)

    assert capability is None
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "cache capability resolution failed" in record.getMessage().lower()
    ]
    assert warnings
    assert "preset lookup exploded" in warnings[0].getMessage()
