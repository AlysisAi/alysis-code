"""Tests for the full-screen TUI configuration menu.

The :class:`ConfigFlow` is driven synchronously (no terminal) for the bulk of the
coverage — it reuses ``config_menu.ConfigMenuState`` so these assert the *menu
walk* (navigation + section mutations + save) rather than the config logic itself.
A single headless ``run_tui`` smoke checks the overlay is wired into the chat app:
bare ``/config`` opens it (and is not routed to the command runner), ``q`` closes.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from alysis_code.cli_impl.tui import config_flow as flow_mod
from alysis_code.cli_impl.tui.app import ConfigReloadOutcome
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import (
    AppConfig,
    load_config,
    load_persisted_profile_keys,
)
from alysis_code.profile_presets import PROFILE_PRESETS, make_profile_from_preset
from alysis_code.profiles import ProfileSpec
from alysis_code.provider_auth import ProviderAccountStatus

# --------------------------------------------------------------------------- helpers


def _config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    for var in (
        "ALYSIS_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "ALYSIS_SHELL_SANDBOX_MODE",
        "ALYSIS_VERIFY_SANDBOX_MODE",
    ):
        monkeypatch.delenv(var, raising=False)


def _cfg(**overrides: Any) -> AppConfig:
    base = {"model": "gpt-4o", "base_url": "https://api.openai.com/v1"}
    base.update(overrides)
    return AppConfig(**base)


def _cfg_with_profiles(active: str = "openai") -> AppConfig:
    openai = make_profile_from_preset(
        next(p for p in PROFILE_PRESETS if p.key == "openai"), name="openai"
    )
    anthropic = make_profile_from_preset(
        next(p for p in PROFILE_PRESETS if p.key == "anthropic"), name="anthropic"
    )
    cfg = AppConfig(model="gpt-4o")
    cfg.extra_fields = {
        "profiles": {"openai": openai.to_dict(), "anthropic": anthropic.to_dict()},
        "active_profile": active,
    }
    return cfg


def _cfg_with_subscription_profile() -> AppConfig:
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-5.4",
        reasoning_effort="high",
    )
    cfg = AppConfig(model=profile.default_model)
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    return cfg


# --------------------------------------------------------------------------- menu


def test_menu_lists_sections_and_actions():
    flow = ConfigFlow(cfg=_cfg())
    scr = flow.screen()
    assert scr.stage == "menu" and scr.mode == "list"
    values = [r.value for r in scr.rows if r.kind == "item"]
    assert values == [
        "workspace",
        "sandbox",
        "personas",
        "execution",
        "profile",
        "default",
        "api_key",
        "web_search",
        "cache",
        "advanced",
        "__save__",
        "__cancel__",
    ]
    # Grouped layout: three section captions, no digit numbering, no in-body
    # hint (the overlay footer owns the key legend).
    assert [r.label for r in scr.rows if r.kind == "header"] == ["Workspace", "Model", "Behavior"]
    assert scr.numbered is False and scr.hint == ""
    # The cursor starts on the first real item, never on a header row.
    assert scr.rows[scr.index].value == "workspace"
    assert not flow.state.dirty


def test_execution_backend_flow_creates_native_subscription_profile():
    flow = ConfigFlow(cfg=_cfg(model="native-model"))

    flow.choose("execution")
    assert flow.stage == "execution_backend"
    access_screen = flow.screen()
    assert access_screen.title == "Model Access"
    assert access_screen.subtitle == "How would you like to connect Alysis Code to AI models?"
    assert [row.value for row in access_screen.rows] == ["native", "delegated"]
    assert [row.label for row in access_screen.rows] == [
        "Use an API key",
        "Use an AI subscription",
    ]
    assert all("Codex" not in row.label for row in access_screen.rows)

    flow.choose("delegated")
    assert flow.stage == "execution_runtime"
    runtime_screen = flow.screen()
    assert runtime_screen.title == "AI Subscription"
    assert "openai-codex" in [row.value for row in runtime_screen.rows]

    flow.choose("openai-codex")
    assert flow.stage == "subscription_account"
    flow.choose("back")
    assert flow.stage == "menu"
    assert flow.state.execution_backend == "delegated"
    assert flow.state.execution_runtime == "openai-codex"
    assert flow.state.agent_runtimes == {}
    assert flow.state.active_profile == "chatgpt-codex"
    rows = {row.value: row for row in flow.screen().rows}
    assert "inactive" not in rows["profile"].label.lower()
    # Status lives in the description only — the label stays clean.
    assert rows["api_key"].label == "API Key"
    assert rows["api_key"].description == "not used"
    assert "inactive" not in rows["default"].label.lower()
    assert rows["execution"].label == "Model Access"
    # Grouped menu shows the one key fact; the "AI subscription ·" prefix moved
    # into the Model Access section screen.
    assert rows["execution"].description == "ChatGPT Codex subscription"


def test_execution_backend_flow_saves_native_subscription_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _config_env(tmp_path, monkeypatch)
    cfg = _cfg(model="native-model", base_url="https://native.example/v1")
    flow = ConfigFlow(cfg=cfg)

    flow.choose("execution")
    flow.choose("delegated")
    flow.choose("openai-codex")
    flow.choose("back")
    flow.choose("__save__")
    flow.run_busy()

    saved = load_config()
    assert flow.stage == "done" and flow.saved is True
    assert saved.execution.backend == "native"
    assert saved.execution.runtime is None
    assert saved.agent_runtimes == {}
    assert saved.extra_fields["active_profile"] == "chatgpt-codex"
    assert saved.extra_fields["profiles"]["chatgpt-codex"]["auth_provider"] == "openai-codex"
    assert saved.model == ""
    assert saved.base_url == "https://chatgpt.com/backend-api/codex"
    assert saved.extra_fields["subscription_model_selection_required"] == "openai-codex"


def test_model_access_manages_subscription_account(monkeypatch: pytest.MonkeyPatch):
    class FakeSubscriptionAuth:
        def __init__(self) -> None:
            self.connected = False
            self.login_calls = 0
            self.logout_calls = 0

        def account_status(self) -> ProviderAccountStatus:
            return ProviderAccountStatus(
                connected=self.connected,
                account_label="developer@example.test" if self.connected else None,
                detail="Connected." if self.connected else "Not connected.",
            )

        def login(self, *, method: str, output_write) -> ProviderAccountStatus:  # type: ignore[no-untyped-def]
            assert method == "browser"
            self.login_calls += 1
            self.connected = True
            return self.account_status()

        def logout(self) -> ProviderAccountStatus:
            self.logout_calls += 1
            self.connected = False
            return ProviderAccountStatus(connected=False, detail="Disconnected locally.")

    adapter = FakeSubscriptionAuth()
    monkeypatch.setattr(flow_mod, "create_provider_auth", lambda _provider_id: adapter)
    flow = ConfigFlow(cfg=_cfg_with_subscription_profile())

    flow.choose("execution")
    flow.choose("delegated")
    flow.choose("openai-codex")
    assert flow.stage == "subscription_account"
    assert [row.value for row in flow.screen().rows] == ["connect", "back"]

    flow.choose("connect")
    assert flow.stage == "subscription_connecting"
    flow.run_busy()
    assert flow.stage == "subscription_account"
    assert adapter.login_calls == 1
    assert [row.value for row in flow.screen().rows] == ["connect", "disconnect", "back"]

    flow.choose("disconnect")
    assert flow.stage == "subscription_disconnect_confirm"
    flow.confirm(True)
    assert flow.stage == "subscription_disconnecting"
    flow.run_busy()
    assert flow.stage == "subscription_account"
    assert adapter.logout_calls == 1
    assert [row.value for row in flow.screen().rows] == ["connect", "back"]


def test_advanced_submenu_holds_subagent_and_forge():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("advanced")
    assert flow.stage == "advanced" and flow.current_mode() == "list"
    values = [r.value for r in flow.screen().rows]
    assert values == ["subagents", "forge", "back"]
    flow.choose("back")
    assert flow.stage == "menu"


def test_legacy_router_override_is_invisible_and_not_counted_as_override():
    base_flow = ConfigFlow(cfg=_cfg())
    cfg = _cfg()
    cfg.extra_fields = {"role_models": {"router": "cheap-router"}}
    flow = ConfigFlow(cfg=cfg)

    # The deprecated legacy key never surfaces: no menu row and no override count.
    assert "router" not in [row.value for row in flow.screen().rows]
    assert flow._advanced_summary() == base_flow._advanced_summary()
    flow.choose("advanced")
    subagent_row = next(row for row in flow.screen().rows if row.value == "subagents")
    base_flow.choose("advanced")
    base_subagent_row = next(row for row in base_flow.screen().rows if row.value == "subagents")
    assert subagent_row.description == base_subagent_row.description


def test_menu_choose_save_and_cancel_route():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("__cancel__")  # clean → exits immediately
    assert flow.stage == "done" and flow.success is False

    flow2 = ConfigFlow(cfg=_cfg())
    flow2.choose("__save__")
    assert flow2.stage == "saving" and flow2.current_mode() == "busy"


# --------------------------------------------------------------------------- sandbox


def test_sandbox_section_sets_field_and_returns_to_menu():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("sandbox")
    assert flow.stage == "sandbox"
    # The picker pre-selects the current mode (strict by default).
    assert [r.current for r in flow.screen().rows] == [True, False, False]
    flow.choose("off")
    assert flow.stage == "menu"
    assert flow.state.fields["sandbox_mode"] == "off"
    assert flow.status_tone == "warn"  # "off" warns
    assert flow.state.dirty


# --------------------------------------------------------------------------- default model


def test_default_model_full_flow():
    flow = ConfigFlow(cfg=_cfg(model="gpt-4o"))
    flow.choose("default")
    assert flow.stage == "model"
    flow.choose(flow.screen().rows[0].value)  # current model row
    assert flow.stage == "model_base_url"
    flow.submit_input("")  # keep current base_url
    assert flow.stage == "model_thinking"
    flow.choose("high")
    assert flow.stage == "model_timeout"
    flow.submit_input("45")
    assert flow.stage == "menu"
    assert flow.state.fields["llm_timeout_s"] == "45"
    assert flow.state.thinking_label == "high"


def test_default_model_picker_preselects_current_subscription_model(monkeypatch):
    cfg = _cfg_with_subscription_profile()
    profile_data = cfg.extra_fields["profiles"]["chatgpt-codex"]
    profile_data["default_model"] = "gpt-5.6-luna"
    cfg.model = "gpt-5.6-luna"
    monkeypatch.setattr(
        flow_mod,
        "_default_model_rows",
        lambda _state: [
            ("gpt-5.6-sol", "GPT 5.6 Sol", ""),
            ("gpt-5.6-luna", "GPT 5.6 Luna", ""),
            (flow_mod._CUSTOM_MODEL_VALUE, "Custom model…", ""),
        ],
    )

    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")

    screen = flow.screen()
    assert flow.stage == "model"
    assert flow.index == 1
    assert screen.rows[flow.index].value == "gpt-5.6-luna"
    assert [row.current for row in screen.rows] == [False, True, False]

    flow.choose_current()
    assert flow.state.fields["model"] == "gpt-5.6-luna"


def test_default_model_custom_path():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("default")
    flow.choose(flow_mod._CUSTOM_MODEL_VALUE)
    assert flow.stage == "custom_model"
    flow.submit_input("my-custom-model")
    assert flow.stage == "model_base_url"
    assert flow.state.fields["model"] == "my-custom-model"


def test_model_timeout_rejects_non_positive():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("default")
    flow.choose(flow.screen().rows[0].value)
    flow.submit_input("")  # base_url
    flow.choose("auto")  # thinking
    flow.submit_input("0")  # invalid timeout
    assert flow.stage == "model_timeout"  # stayed
    assert flow.status_tone == "err"


# --------------------------------------------------------------------------- context & cache


def test_web_search_flow_and_save(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg())

    flow.choose("web_search")
    assert flow.stage == "web_search_policy"
    assert [row.value for row in flow.screen().rows] == ["auto", "off"]
    flow.choose("off")
    assert flow.stage == "web_search_mode"
    assert [row.value for row in flow.screen().rows] == [
        "auto",
        "external",
        "native",
        "off",
    ]
    flow.choose("external")

    assert flow.stage == "menu"
    assert flow.state.fields["web_search_policy"] == "off"
    assert flow.state.fields["web_search_mode"] == "external"
    flow.choose("__save__")
    flow.run_busy()

    saved = load_config()
    assert flow.stage == "done" and flow.saved is True
    assert saved.web_search_policy == "off"
    assert saved.web_search_mode == "external"


def test_context_cache_full_flow_and_save(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg())

    flow.choose("cache")
    assert flow.stage == "cache_mode"
    flow.choose("auto")
    assert flow.stage == "cache_key"
    flow.submit_input("repo-main")
    assert flow.stage == "cache_retention"
    flow.submit_input("24h")
    assert flow.stage == "cache_anthropic_enabled"
    flow.choose("true")
    assert flow.stage == "cache_anthropic_ttl"
    flow.choose("1h")
    assert flow.stage == "cache_compaction_enabled"
    flow.choose("false")
    assert flow.stage == "cache_compaction_min_trigger"
    flow.submit_input("0.78")

    assert flow.stage == "menu"
    assert flow.state.fields["prompt_cache_mode"] == "auto"
    assert flow.state.fields["prompt_cache_key"] == "repo-main"
    assert flow.state.fields["prompt_cache_retention"] == "24h"
    assert flow.state.fields["anthropic_prompt_cache_enabled"] == "true"
    assert flow.state.fields["anthropic_prompt_cache_ttl"] == "1h"
    assert flow.state.fields["cache_aware_compaction"] == "false"
    assert flow.state.fields["cache_aware_min_trigger_ratio"] == "0.78"
    flow.choose("__save__")
    flow.run_busy()

    saved = load_config()
    assert flow.stage == "done" and flow.saved is True
    assert saved.prompt_cache_mode == "auto"
    assert saved.prompt_cache_key == "repo-main"
    assert saved.prompt_cache_retention == "24h"
    assert saved.anthropic_prompt_cache_enabled is True
    assert saved.anthropic_prompt_cache_ttl == "1h"
    assert saved.extra_fields["compaction"]["cache_aware_compaction"] is False
    assert saved.extra_fields["compaction"]["cache_aware_min_trigger_ratio"] == 0.78


def test_context_cache_screen_shows_effective_cache_capability():
    flow = ConfigFlow(cfg=_cfg_with_profiles(active="openai"))

    flow.choose("cache")
    screen = flow.screen()

    assert screen.stage == "cache_mode"
    # Humanized subtitle: the raw "Effective: strategy=…; emits=…" debug dump
    # is gone; a supported provider still names its caching strategy.
    assert "Effective:" not in screen.subtitle
    assert screen.subtitle.startswith("Provider caching:")
    assert "openai_prompt_cache" in screen.subtitle


def test_context_cache_clear_inputs():
    flow = ConfigFlow(
        cfg=_cfg(
            prompt_cache_mode="manual",
            prompt_cache_key="repo-main",
            prompt_cache_retention="24h",
        )
    )

    flow.choose("cache")
    flow.choose("manual")
    flow.submit_input("clear")
    flow.submit_input("clear")

    assert flow.state.fields["prompt_cache_key"] == ""
    assert flow.state.fields["prompt_cache_retention"] == ""


# --------------------------------------------------------------------------- routing


def test_no_routing_stages_remain_in_flow():
    # The pre-turn semantic router is gone; the TUI must expose no routing or
    # router-model stages at all.
    assert not any(stage.startswith("limits_") for stage in flow_mod._STAGE_MODE)
    assert not any("router" in stage or "routing" in stage for stage in flow_mod._STAGE_MODE)


# --------------------------------------------------------------------------- subagent / forge


def test_subagent_roles_list_and_single_role_edit():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("advanced")
    flow.choose("subagents")
    # A role list now, not a forced walk: one row per role plus Back.
    scr = flow.screen()
    assert flow.stage == "subagent_roles"
    values = [r.value for r in scr.rows]
    assert values[-1] == "back"
    assert "coding" in values
    flow.choose("coding")
    assert flow.stage == "subagent_field" and flow.field_key() == "subagent_field:0"
    flow.submit_input("mini")  # coding model
    assert flow.field_key() == "subagent_field:1"  # temperature step for this role
    assert flow.state.role_models.get("coding") == "mini"
    flow.submit_input("")  # blank keeps the temperature
    assert flow.stage == "subagent_roles"  # back on the role list, not Advanced
    rows = {r.value: r for r in flow.screen().rows}
    assert "mini" in rows["coding"].description
    # "clear" removes an override again.
    flow.choose("coding")
    flow.submit_input("clear")
    flow.submit_input("")
    assert flow.state.role_models.get("coding") == ""


def test_subagent_temperature_validation():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("advanced")
    flow.choose("subagents")
    flow.choose("coding")
    flow.submit_input("")  # coding model -> step to coding temp
    assert flow.field_key() == "subagent_field:1"
    flow.submit_input("-1")  # invalid temperature
    assert flow.stage == "subagent_field" and flow.field_key() == "subagent_field:1"
    assert flow.status_tone == "err"


def test_save_preserves_unexposed_role_model_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Keys the TUI does not expose — including the deprecated legacy "router"
    # key — must survive an unrelated edit-and-save round trip.
    _config_env(tmp_path, monkeypatch)
    cfg = _cfg_with_profiles(active="openai")
    cfg.extra_fields["role_models"] = {
        "router": "legacy-router-model",
        "comprehension": "vision-reader",
    }
    flow = ConfigFlow(cfg=cfg)

    flow.choose("advanced")
    flow.choose("subagents")
    flow.choose("coding")
    flow.submit_input("mini")
    flow.submit_input("")
    flow.back()
    flow.back()
    flow.choose("__save__")
    flow.run_busy()

    assert flow.stage == "done" and flow.saved is True
    assert load_config().extra_fields["role_models"] == {
        "comprehension": "vision-reader",
        "router": "legacy-router-model",
        "coding": "mini",
    }


def test_subscription_subagent_overrides_hide_unsupported_temperature_controls():
    flow = ConfigFlow(cfg=_cfg_with_subscription_profile())
    flow.choose("advanced")
    flow.choose("subagents")
    flow.choose("coding")

    # Single-step edit (no temperature) with the subscription note visible.
    assert all(kind == "model" for _role, kind in flow._sub_steps)
    assert "temperature is managed by the AI subscription" in flow.screen().subtitle

    flow.submit_input("subscription-mini")
    assert flow.stage == "subagent_roles"
    assert flow.state.role_models.get("coding") == "subscription-mini"


def test_subagent_esc_reverts():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("advanced")
    flow.choose("subagents")
    flow.choose("coding")
    flow.submit_input("changed")
    assert flow.state.role_models.get("coding") == "changed"
    flow.back()  # Esc cancels this role's edit
    assert flow.stage == "subagent_roles"
    assert flow.state.role_models.get("coding") == ""  # reverted
    flow.back()
    assert flow.stage == "advanced"


def test_forge_overrides_edit_roles_individually():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("advanced")
    flow.choose("forge")
    assert flow.stage == "forge_roles"
    values = [r.value for r in flow.screen().rows]
    assert "coding" in values and values[-1] == "back"
    # The removed semantic router's role is no longer an editable forge role.
    assert "router" not in values
    flow.choose("coding")
    assert flow.stage == "forge_field"
    flow.submit_input("forge-model")
    assert flow.stage == "forge_roles"
    assert flow.state.forge_role_models.get("coding") == "forge-model"


# --------------------------------------------------------------------------- api key


def test_api_key_set():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("api_key")
    assert flow.stage == "api_key"
    flow.submit_input("sk-secret-123")
    assert flow.stage == "menu"
    assert flow.state.new_api_key == "sk-secret-123"


def test_api_key_blank_keeps_current():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("api_key")
    flow.submit_input("")
    assert flow.stage == "menu"
    assert flow.state.new_api_key == ""


def test_api_key_clear_confirm():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("api_key")
    flow.submit_input("clear")
    assert flow.stage == "api_key_clear_confirm"
    flow.confirm(True)
    assert flow.stage == "menu"
    assert flow.state.clear_stored_key_confirmed is True


# --------------------------------------------------------------------------- provider profile


def test_provider_switch_changes_active():
    flow = ConfigFlow(cfg=_cfg_with_profiles(active="openai"))
    flow.choose("profile")
    flow.choose("switch")
    assert flow.stage == "provider_switch"
    flow.choose("anthropic")
    assert flow.stage == "provider"
    assert flow.state.active_profile == "anthropic"


def test_provider_add_preset_with_base_url():
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_preset")
    assert flow.stage == "provider_add_preset"
    flow.choose("openai")  # has a base_url → no URL prompt
    assert flow.stage == "provider_preset_name"
    flow.submit_input("")  # default name = preset key
    assert flow.stage == "api_key"  # now chains into the guided API key → model steps
    assert "openai" in flow.state.profiles
    assert flow.state.active_profile == "openai"


def test_provider_add_preset_cannot_overwrite_subscription_profile():
    flow = ConfigFlow(cfg=_cfg_with_subscription_profile())
    flow.choose("profile")
    flow.choose("add_preset")
    flow.choose("openai-responses")
    flow.submit_input("chatgpt-codex")

    assert flow.stage == "provider_preset_name"
    assert flow.status_tone == "err"
    assert "cannot be overwritten through generic profile settings" in flow.status
    preserved = ProfileSpec.from_dict(
        "chatgpt-codex",
        flow.state.profiles["chatgpt-codex"],
    )
    assert preserved.auth_provider == "openai-codex"
    assert preserved.default_model == "gpt-5.4"

    flow.submit_input("openai-key")
    assert flow.stage == "api_key"
    assert "openai-key" in flow.state.profiles


def test_provider_add_preset_surfaces_hosted_providers_with_advanced_branch():
    # The TUI /config "Add from preset" picker surfaces every hosted provider
    # (DeepSeek, OpenRouter, …) directly; only local, compatibility, custom, and
    # legacy presets stay behind the "Advanced" branch. Asserts displayed rows.
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_preset")
    assert flow.stage == "provider_add_preset"

    values = [r.value for r in flow.screen().rows]
    assert "deepseek" in values  # hosted providers now on the primary picker…
    assert "ollama" not in values  # …local endpoints stay behind the advanced branch
    assert flow_mod._ADVANCED_PROVIDER_PRESETS_VALUE in values

    flow.choose(flow_mod._ADVANCED_PROVIDER_PRESETS_VALUE)
    assert flow.stage == "provider_add_preset_advanced"
    advanced_values = [r.value for r in flow.screen().rows]
    assert "deepseek" not in advanced_values  # promoted to the primary picker
    assert "ollama" in advanced_values

    flow.choose("ollama")
    assert flow.stage == "provider_preset_name"
    assert flow._pending_preset is not None
    assert flow._pending_preset.key == "ollama"


def test_provider_add_preset_advanced_back_returns_to_primary():
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_preset")
    flow.choose(flow_mod._ADVANCED_PROVIDER_PRESETS_VALUE)
    assert flow.stage == "provider_add_preset_advanced"
    flow.back()
    assert flow.stage == "provider_add_preset"


def test_add_preset_chains_into_api_key_then_model():
    # After adding a provider preset, the flow guides the user straight through
    # API key → default-model pick, landing fully configured — not just
    # "Profile added" back at the menu.
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_preset")
    flow.choose("deepseek")  # hosted providers are on the primary picker now
    flow.submit_input("")  # accept default profile name (deepseek has a base_url)

    assert flow.stage == "api_key"
    assert "deepseek" in flow.state.profiles
    assert flow.state.active_profile == "deepseek"

    flow.submit_input("sk-deepseek-test")
    # Chained to the model picker, now scoped to DeepSeek's models.
    assert flow.stage == "model"
    model_values = [r.value for r in flow.screen().rows]
    assert "deepseek-v4-pro" in model_values

    flow.choose("deepseek-v4-pro")
    # Done: back on the provider screen, fully configured, chain cleared.
    assert flow.stage == "provider"
    assert flow.state.fields.get("model") == "deepseek-v4-pro"
    assert flow._preset_setup_chain is False


def test_add_nvidia_preset_chains_through_model_specific_reasoning(monkeypatch):
    from alysis_code.cli_impl import config_menu as config_menu_mod

    monkeypatch.setattr(
        config_menu_mod,
        "_provider_models_for_state",
        lambda _state, _preset: (
            config_menu_mod.ProviderModelOption(
                id="deepseek-ai/deepseek-v4-pro",
                label="deepseek-ai/deepseek-v4-pro",
            ),
        ),
    )
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_preset")
    flow.choose("nvidia")
    flow.submit_input("")

    assert flow.stage == "api_key"
    flow.submit_input("nvapi-test")
    assert flow.stage == "model"
    assert "deepseek-ai/deepseek-v4-pro" in {row.value for row in flow.screen().rows}

    flow.choose("deepseek-ai/deepseek-v4-pro")
    assert flow.stage == "model_thinking"
    assert [row.value for row in flow.screen().rows] == ["off", "high", "max", "auto"]

    flow.choose("max")
    assert flow.stage == "provider"
    assert flow.state.fields.get("model") == "deepseek-ai/deepseek-v4-pro"
    assert flow.state.thinking_label == "max"
    assert flow._preset_setup_chain is False


def test_add_preset_chain_allows_skipping_api_key():
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_preset")
    flow.choose("deepseek")  # hosted providers are on the primary picker now
    flow.submit_input("")
    assert flow.stage == "api_key"
    flow.submit_input("")  # skip the key — still guided onward to the model pick
    assert flow.stage == "model"


def test_api_key_menu_action_still_returns_to_menu():
    # Guard: outside the add-provider chain, the API-key step still returns to menu.
    flow = ConfigFlow(cfg=_cfg_with_profiles(active="openai"))
    flow.choose("api_key")
    assert flow.stage == "api_key"
    flow.submit_input("sk-standalone")
    assert flow.stage == "menu"
    assert flow._preset_setup_chain is False


def test_provider_add_custom():
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_custom")
    assert flow.stage == "provider_custom_name"
    flow.submit_input("local")
    assert flow.stage == "provider_custom_url"
    flow.submit_input("http://localhost:1234/v1")
    assert flow.stage == "provider_custom_headers"
    flow.submit_input("")  # no headers
    assert flow.stage == "provider"
    assert "local" in flow.state.profiles
    assert flow.state.profiles["local"]["base_url"] == "http://localhost:1234/v1"


def test_provider_add_custom_cannot_overwrite_subscription_profile():
    flow = ConfigFlow(cfg=_cfg_with_subscription_profile())
    flow.choose("profile")
    flow.choose("add_custom")
    flow.submit_input("chatgpt-codex")
    flow.submit_input("http://localhost:1234/v1")
    flow.submit_input("")

    assert flow.stage == "provider_custom_name"
    assert flow.status_tone == "err"
    assert "cannot be overwritten through generic profile settings" in flow.status
    preserved = ProfileSpec.from_dict(
        "chatgpt-codex",
        flow.state.profiles["chatgpt-codex"],
    )
    assert preserved.auth_provider == "openai-codex"
    assert preserved.base_url == "https://chatgpt.com/backend-api/codex"


def test_provider_add_custom_rejects_empty_url():
    flow = ConfigFlow(cfg=AppConfig(model="x"))
    flow.choose("profile")
    flow.choose("add_custom")
    flow.submit_input("local")
    flow.submit_input("")  # url required
    assert flow.stage == "provider_custom_url"
    assert flow.status_tone == "err"


def test_provider_edit_secret_guard():
    flow = ConfigFlow(cfg=_cfg_with_profiles())
    flow.choose("profile")
    flow.choose("edit")
    assert flow.stage == "provider_edit" and flow.field_key() == "provider_edit:0"
    # base_url field: pasting a key-looking value is refused.
    flow.submit_input("sk-abcdef0123456789abcdef")
    assert flow.stage == "provider_edit" and flow.field_key() == "provider_edit:0"
    assert flow.status_tone == "warn"
    # 'force' keeps the flagged candidate and advances (edits accumulate locally
    # and are applied to the profile only when the whole sequence finalizes).
    flow.submit_input("force")
    assert flow.field_key() == "provider_edit:1"
    assert flow._edit_values["base_url"].startswith("sk-")


def test_provider_edit_full_walk_updates_profile():
    flow = ConfigFlow(cfg=_cfg_with_profiles())
    flow.choose("profile")
    flow.choose("edit")
    flow.submit_input("https://example.com/v1")  # base_url
    guard = 0
    while flow.stage == "provider_edit":
        flow.submit_input("")  # keep the rest
        guard += 1
        assert guard < 20
    assert flow.stage == "provider"
    assert flow.state.profiles[flow.state.active_profile]["base_url"] == "https://example.com/v1"


def test_provider_remove_confirm():
    flow = ConfigFlow(cfg=_cfg_with_profiles(active="openai"))
    flow.choose("profile")
    flow.choose("remove")
    assert flow.stage == "provider_remove"
    flow.choose("anthropic")
    assert flow.stage == "provider_remove_confirm"
    flow.confirm(True)
    assert flow.stage == "provider"
    assert "anthropic" not in flow.state.profiles


# --------------------------------------------------------------------------- save / cancel


def test_save_persists_sandbox_change(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("sandbox")
    flow.choose("off")
    assert flow.state.dirty
    flow.choose("__save__")
    assert flow.current_mode() == "busy"
    flow.run_busy()
    assert flow.stage == "done" and flow.success is True and flow.saved is True
    assert flow.changes_count >= 1
    from alysis_code.sandbox_settings import sandbox_mode_from_config

    assert sandbox_mode_from_config(load_config()) == "off"


def test_save_persists_profile_api_key(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg_with_profiles(active="openai"))
    flow.choose("api_key")
    flow.submit_input("sk-persist-me")
    flow.choose("__save__")
    flow.run_busy()
    assert flow.stage == "done" and flow.saved is True
    assert load_persisted_profile_keys().get("openai") == "sk-persist-me"


def test_save_validation_failure_returns_to_menu(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg())
    flow.state.fields["llm_timeout_s"] = "-5"  # invalid, bypassing the input guard
    flow._goto("saving")
    flow.run_busy()
    assert flow.stage == "menu"
    assert flow.status_tone == "err"
    assert flow.saved is False and flow.success is None


def test_cancel_when_dirty_confirms():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("sandbox")
    flow.choose("off")  # dirty
    flow.request_cancel()
    assert flow.stage == "cancel_confirm"
    flow.confirm(False)  # keep editing
    assert flow.stage == "menu"
    flow.request_cancel()
    flow.confirm(True)  # discard
    assert flow.stage == "done" and flow.success is False


def test_cancel_when_clean_exits_directly():
    flow = ConfigFlow(cfg=_cfg())
    flow.request_cancel()
    assert flow.stage == "done" and flow.success is False


def test_tui_save_keeps_staged_api_key_bound_to_original_profile(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg_with_profiles(active="openai"))
    flow.state.set_field("new_api_key", "openai-only-secret")
    flow.state.set_active_profile_name("anthropic")

    flow._goto("saving")
    flow.run_busy()

    stored = load_persisted_profile_keys()
    assert stored["openai"] == "openai-only-secret"
    assert "anthropic" not in stored


def test_cancel_no_resumes_to_current_subflow_stage():
    # Ctrl+C inside a sub-flow → discard confirm; answering "No" returns to where
    # the user was (the sub-flow), not the top menu.
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("sandbox")
    flow.choose("off")  # make it dirty, back at menu
    flow.choose("default")  # enter the model sub-flow
    assert flow.stage == "model"
    flow.request_cancel()
    assert flow.stage == "cancel_confirm"
    flow.confirm(False)
    assert flow.stage == "model"  # resumed, not snapped to menu


# --------------------------------------------------------------------------- save split (worker/UI handoff)


def test_perform_save_records_outcome_without_transition(monkeypatch, tmp_path):
    # The overlay runs perform_save() on a worker (no renderer-visible stage write),
    # then apply_save_outcome() on the UI thread does the transition.
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("sandbox")
    flow.choose("off")
    flow._goto("saving")
    flow.perform_save()
    assert flow.stage == "saving"  # perform_save did NOT transition
    assert flow._save_outcome == ("saved", "")
    assert flow.saved is False
    flow.apply_save_outcome()
    assert flow.stage == "done" and flow.saved is True and flow.success is True


def test_perform_save_validation_failure_outcome(monkeypatch, tmp_path):
    _config_env(tmp_path, monkeypatch)
    flow = ConfigFlow(cfg=_cfg())
    flow.state.fields["llm_timeout_s"] = "-5"  # invalid
    flow._goto("saving")
    flow.perform_save()
    assert flow._save_outcome is not None and flow._save_outcome[0] == "error"
    assert flow.stage == "saving"  # not transitioned yet
    flow.apply_save_outcome()
    assert flow.stage == "menu" and flow.status_tone == "err" and flow.saved is False


def test_set_save_failure_surfaces_on_apply():
    flow = ConfigFlow(cfg=_cfg())
    flow._goto("saving")
    flow.set_save_failure("disk on fire")
    flow.apply_save_outcome()
    assert flow.stage == "menu" and flow.status_tone == "err"
    assert "disk on fire" in flow.status


# --------------------------------------------------------------------------- project / workspace


def test_thinking_labels_follow_reasoning_contracts():
    from alysis_code.cli_impl.tui.config_flow import _thinking_labels_allowed_by_contract
    from alysis_code.reasoning_contracts import (
        UNKNOWN_CONTRACT,
        reasoning_contract_for,
    )

    labels = ["off", "low", "medium", "high", "max"]
    # kimi-code k3: "off" stays visible (it swaps the model and must be warned
    # about, not hidden); "medium" is outside the allowed effort set.
    k3 = reasoning_contract_for("moonshot", "k3", preset_key="kimi-code")
    assert _thinking_labels_allowed_by_contract(k3, labels, current="high") == [
        "off",
        "low",
        "high",
        "max",
    ]
    # moonshot k2.7-code: always-on with a hard 400 on disable — "off" is
    # hidden; its values describe the toggle wire, so efforts are untouched.
    k27 = reasoning_contract_for("moonshot", "kimi-k2.7-code")
    assert _thinking_labels_allowed_by_contract(k27, labels, current="high") == [
        "low",
        "medium",
        "high",
        "max",
    ]
    # The current selection is never hidden, even when out of contract.
    assert "medium" in _thinking_labels_allowed_by_contract(k3, labels, current="medium")
    # Unknown contract leaves the list untouched.
    assert _thinking_labels_allowed_by_contract(UNKNOWN_CONTRACT, labels, current="off") == labels


def test_menu_has_project_workspace_first():
    flow = ConfigFlow(cfg=_cfg(), current_workspace="/home/me/proj")
    items = [r for r in flow.screen().rows if r.kind == "item"]
    assert items[0].value == "workspace" and items[0].label == "Project"
    assert "proj" in items[0].description  # current workspace name shown


def test_workspace_screen_offers_type_path_and_back():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("workspace")
    assert flow.stage == "workspace"
    values = [r.value for r in flow.screen().rows]
    assert "__type_path__" in values and "back" in values


def test_workspace_set_default_stages_through_save(tmp_path, monkeypatch):
    _config_env(tmp_path, monkeypatch)
    project = tmp_path / "myproject"
    project.mkdir()
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("workspace")
    flow.choose("__type_path__")
    assert flow.stage == "workspace_path"
    flow.submit_input(os.fspath(project))
    assert flow.stage == "workspace_action"
    assert "myproject" in flow._pending_workspace
    flow.choose("set_default")
    assert flow.stage == "menu" and flow.state.dirty
    assert "myproject" in flow.state.default_workspace_path
    # Persists through the normal Save.
    flow._goto("saving")
    flow.run_busy()
    assert flow.stage == "done" and flow.saved is True
    saved = load_config()
    assert "myproject" in str((saved.extra_fields or {}).get("default_workspace_path"))


def test_workspace_switch_now_signals_switch_and_persists(tmp_path, monkeypatch):
    _config_env(tmp_path, monkeypatch)
    project = tmp_path / "other"
    project.mkdir()
    flow = ConfigFlow(cfg=_cfg(), current_workspace=os.fspath(tmp_path))
    flow.choose("workspace")
    flow.choose("__type_path__")
    flow.submit_input(os.fspath(project))
    assert flow.stage == "workspace_action"
    flow.choose("switch")
    assert flow.stage == "switching" and flow.current_mode() == "busy"
    flow.run_busy()
    assert flow.stage == "done" and flow.saved is True
    assert flow.switch_workspace and "other" in flow.switch_workspace
    saved = load_config()
    assert "other" in str((saved.extra_fields or {}).get("default_workspace_path"))


def test_workspace_empty_path_reprompts():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("workspace")
    flow.choose("__type_path__")
    flow.submit_input("   ")
    assert flow.stage == "workspace_path" and flow.status_tone == "err"


def test_workspace_back_returns_to_menu():
    flow = ConfigFlow(cfg=_cfg())
    flow.choose("workspace")
    flow.choose("back")
    assert flow.stage == "menu"


# --------------------------------------------------------------------------- headless wiring smoke


class _FakeSession:
    def __init__(self, surface: Any) -> None:
        self.surface = surface

    def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
        return 0

    def close(self) -> None:
        pass


def _fake_command_runner(calls: list) -> Any:
    def _runner(sess: Any, text: str, width: int):
        calls.append((text, width))
        if text.strip().lower() in {"/exit", "/quit"}:
            return ("exit", "", None, None)
        return ("handled", "", None, None)

    return _runner


def _run_headless(keys: str, **kwargs: Any):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    state = TuiState(model_name="m", username="t")
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return run_tui(state, owl_color=False, input=pipe, output=DummyOutput(), **kwargs)


def _run_live_headless(feed: Any, **kwargs: Any):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    with create_pipe_input() as pipe:
        feed_errors: list[BaseException] = []

        def run_feed() -> None:
            try:
                feed(pipe)
            except BaseException as exc:  # noqa: BLE001 - report feeder failures
                feed_errors.append(exc)
                pipe.send_text("\x03")
                time.sleep(0.05)
                pipe.send_text("\x03")

        feeder = threading.Thread(target=run_feed, daemon=True)
        feeder.start()
        result = run_tui(
            TuiState(model_name="m", username="t"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            **kwargs,
        )
        feeder.join(timeout=2)

    assert not feeder.is_alive()
    assert feed_errors == []
    return result


def test_headless_config_opens_overlay_not_routed_to_runner():
    # Bare /config is intercepted natively: it opens the overlay (the factory is
    # invoked) instead of being echoed as a user line or routed to the runner.
    # Pressing q closes it (clean menu → exits), then /exit leaves.
    calls: list = []
    opened = {"n": 0}

    def _factory():
        opened["n"] += 1
        return ConfigFlow(cfg=_cfg())

    _result, transcript = _run_headless(
        "/config\rq/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        config_flow_factory=_factory,
        background_turns=False,
    )
    assert opened["n"] == 1
    assert ("user", "/config") not in transcript
    assert all(text.strip().lower() != "/config" for text, _w in calls)


def test_headless_config_can_open_on_start():
    opened = {"n": 0}

    def _factory():
        opened["n"] += 1
        return ConfigFlow(cfg=_cfg())

    result, transcript = _run_headless(
        "q/exit\r",
        config_flow_factory=_factory,
        open_config_on_start=True,
    )

    assert opened["n"] == 1
    assert result == "/exit"
    assert transcript == []


def test_config_input_plain_selection_backspace_cuts_and_enter_accepts(monkeypatch):
    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.selection import SelectionType

    from alysis_code.cli_impl.tui import app as app_module

    submitted = []
    captured = {}

    class InputFlow(ConfigFlow):
        def __init__(self):
            super().__init__(cfg=_cfg())
            self.stage = "model_timeout"

        def submit_input(self, text):
            submitted.append(text)
            self.saved = False
            self.stage = "done"

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application

        def select_config_text():
            buffer = application.layout.current_buffer
            buffer.text = "abcd"
            buffer.cursor_position = 4
            buffer.start_selection(selection_type=SelectionType.CHARACTERS)
            buffer.cursor_position = 2

        application.pre_run_callables.append(select_config_text)
        return application

    monkeypatch.setattr(app_module, "Application", capture_application)

    result, _transcript = _run_headless(
        "\x7f\r/exit\r",
        config_flow_factory=InputFlow,
        open_config_on_start=True,
    )

    assert result == "/exit"
    assert submitted == ["ab"]


@pytest.mark.parametrize(
    ("command", "factory_name"),
    [("/config", "config_flow_factory")],
)
def test_config_overlays_open_immediately_during_a_turn(command: str, factory_name: str) -> None:
    started = threading.Event()
    release = threading.Event()
    opened = threading.Event()
    runner_calls: list[str] = []

    class BlockingSession(_FakeSession):
        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = text, cancellation_token
            started.set()
            assert release.wait(timeout=3)
            return 0

    def factory() -> ConfigFlow:
        opened.set()
        return ConfigFlow(cfg=_cfg())

    def runner(_session: Any, text: str, _width: int):
        stripped = text.strip()
        runner_calls.append(stripped)
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text(command + "\r")
        assert opened.wait(timeout=2)
        pipe.send_text("q")
        time.sleep(0.05)
        release.set()
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    kwargs = {factory_name: factory}
    (result, transcript) = _run_live_headless(
        feed,
        session_builder=BlockingSession,
        command_runner=runner,
        background_turns=True,
        **kwargs,
    )

    assert result == "/exit"
    assert command not in runner_calls
    assert ("user", command) not in transcript


def test_mid_turn_config_saves_coalesce_reload_before_queued_turn() -> None:
    started = threading.Event()
    release = threading.Event()
    queued_started = threading.Event()
    opened = [threading.Event(), threading.Event()]
    saved = [threading.Event(), threading.Event()]
    events: list[str] = []
    factory_calls = {"n": 0}
    reload_calls = {"n": 0}

    class BlockingSession(_FakeSession):
        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            events.append(f"turn:{text}")
            if text == "work":
                started.set()
                assert release.wait(timeout=3)
            elif text == "queued":
                queued_started.set()
            return 0

    class SignalingFlow(ConfigFlow):
        def __init__(self, index: int) -> None:
            super().__init__(cfg=_cfg())
            self._test_index = index

        def perform_save(self) -> None:
            self.changes_count = 1
            self._save_outcome = ("saved", "")

        def apply_save_outcome(self) -> None:
            super().apply_save_outcome()
            saved[self._test_index].set()

    def factory() -> ConfigFlow:
        index = factory_calls["n"]
        factory_calls["n"] += 1
        opened[index].set()
        return SignalingFlow(index)

    def on_config_saved() -> bool:
        reload_calls["n"] += 1
        events.append("reload")
        return False

    def runner(_session: Any, text: str, _width: int):
        stripped = text.strip()
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        for index in range(2):
            pipe.send_text("/config\r")
            assert opened[index].wait(timeout=2)
            pipe.send_text("s")
            assert saved[index].wait(timeout=2)
            time.sleep(0.05)
            assert reload_calls["n"] == 0
        pipe.send_text("queued\x11")
        time.sleep(0.05)
        release.set()
        assert queued_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_headless(
        feed,
        session_builder=BlockingSession,
        command_runner=runner,
        config_flow_factory=factory,
        on_config_saved=on_config_saved,
        background_turns=True,
    )

    assert result == "/exit"
    assert reload_calls["n"] == 1
    assert events.index("reload") < events.index("turn:queued")
    assert sum("running session will reload" in text for _role, text in transcript) == 2
    assert any("could not be reloaded" in text for role, text in transcript if role == "error")


@pytest.mark.parametrize("save_before_command", [True, False])
def test_mid_turn_config_reload_and_command_keep_submission_order(
    save_before_command: bool,
) -> None:
    started = threading.Event()
    release = threading.Event()
    queued_started = threading.Event()
    opened = threading.Event()
    saved = threading.Event()
    events: list[str] = []

    class BlockingSession(_FakeSession):
        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            events.append(f"turn:{text}")
            if text == "work":
                started.set()
                assert release.wait(timeout=3)
            elif text == "queued":
                queued_started.set()
            return 0

    class SignalingFlow(ConfigFlow):
        def perform_save(self) -> None:
            self.changes_count = 1
            self._save_outcome = ("saved", "")

        def apply_save_outcome(self) -> None:
            super().apply_save_outcome()
            saved.set()

    def factory() -> ConfigFlow:
        opened.set()
        return SignalingFlow(cfg=_cfg())

    def on_config_saved() -> ConfigReloadOutcome:
        events.append("reload")
        return ConfigReloadOutcome.APPLIED

    def runner(_session: Any, text: str, _width: int):
        stripped = text.strip()
        if stripped == "/exit":
            return "exit", "", None, None
        if stripped == "/model staged-model":
            events.append("command:/model staged-model")
            return "handled", "model applied", None, None
        return "run", "", stripped, {}

    def save_config(pipe: Any) -> None:
        pipe.send_text("/config\r")
        assert opened.wait(timeout=2)
        pipe.send_text("s")
        assert saved.wait(timeout=2)
        time.sleep(0.05)

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        if save_before_command:
            save_config(pipe)
            pipe.send_text("/model staged-model\r")
        else:
            pipe.send_text("/model staged-model\r")
            save_config(pipe)
        pipe.send_text("queued\x11")
        time.sleep(0.05)
        release.set()
        assert queued_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, _transcript = _run_live_headless(
        feed,
        session_builder=BlockingSession,
        command_runner=runner,
        config_flow_factory=factory,
        on_config_saved=on_config_saved,
        background_turns=True,
    )

    assert result == "/exit"
    command_index = events.index("command:/model staged-model")
    reload_index = events.index("reload")
    queued_index = events.index("turn:queued")
    if save_before_command:
        assert reload_index < command_index < queued_index
    else:
        assert command_index < reload_index < queued_index


def test_restart_outcome_discards_queued_turn_without_starting_it() -> None:
    from prompt_toolkit.application.current import get_app

    started = threading.Event()
    release = threading.Event()
    opened = threading.Event()
    saved = threading.Event()
    calls: list[str] = []

    class BlockingSession(_FakeSession):
        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            started.set()
            assert release.wait(timeout=3)
            return 0

    class SignalingFlow(ConfigFlow):
        def perform_save(self) -> None:
            self.changes_count = 1
            self._save_outcome = ("saved", "")

        def apply_save_outcome(self) -> None:
            super().apply_save_outcome()
            saved.set()

    def factory() -> ConfigFlow:
        opened.set()
        return SignalingFlow(cfg=_cfg())

    def on_config_saved() -> ConfigReloadOutcome:
        get_app().exit(result=("restart_config", "/workspace"))
        return ConfigReloadOutcome.RESTART

    def runner(_session: Any, text: str, _width: int):
        stripped = text.strip()
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("/config\r")
        assert opened.wait(timeout=2)
        pipe.send_text("s")
        assert saved.wait(timeout=2)
        pipe.send_text("must not start\x11")
        time.sleep(0.05)
        release.set()

    (result, transcript) = _run_live_headless(
        feed,
        session_builder=BlockingSession,
        command_runner=runner,
        config_flow_factory=factory,
        on_config_saved=on_config_saved,
        background_turns=True,
    )

    assert result == ("restart_config", "/workspace")
    assert calls == ["work"]
    assert any(
        "Discarded 1 pending item because this session is closing" in text
        for role, text in transcript
        if role == "warn"
    )
