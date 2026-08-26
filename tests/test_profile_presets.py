from __future__ import annotations

import re

from alysis_code.llm.cache_capabilities import resolve_effective_cache_capability
from alysis_code.profile_presets import (
    PROFILE_PRESETS,
    advanced_provider_selection_presets,
    canonical_model_alias_for_preset,
    convert_profile_to_preset,
    find_preset_for_base_url,
    find_preset_for_profile,
    get_preset,
    make_profile_from_preset,
    preset_selection_label,
    provider_selection_presets,
    target_preset_for_profile_conversion,
)
from alysis_code.profiles import ProfileSpec


def test_at_least_15_presets_registered() -> None:
    assert len(PROFILE_PRESETS) >= 15


def test_each_preset_has_non_empty_label_and_protocol() -> None:
    for preset in PROFILE_PRESETS:
        assert preset.label
        assert preset.protocol in {
            "anthropic_messages",
            "gemini_generate_content",
            "gemini_interactions",
            "openai_compat",
            "openai_responses",
        }


def test_openai_responses_preset_is_explicit_native_opt_in() -> None:
    preset = get_preset("openai-responses")

    assert preset is not None
    assert preset.label == "OpenAI Responses"
    assert preset.protocol == "openai_responses"
    assert preset.base_url == "https://api.openai.com/v1"
    assert preset.web_search_adapter == "openai_responses"


def test_anthropic_preset_is_native_by_default() -> None:
    preset = get_preset("anthropic")

    assert preset is not None
    assert preset.label == "Anthropic Claude"
    assert preset.protocol == "anthropic_messages"
    assert preset.base_url == "https://api.anthropic.com/v1"
    assert preset.web_search_adapter == "anthropic_messages"


def test_gemini_preset_is_native_by_default() -> None:
    preset = get_preset("gemini")

    assert preset is not None
    assert preset.label == "Google Gemini"
    assert preset.protocol == "gemini_generate_content"
    assert preset.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert preset.web_search_adapter == "gemini_grounding"


def test_gemini_interactions_is_not_a_normal_provider_preset() -> None:
    assert all(preset.protocol != "gemini_interactions" for preset in PROFILE_PRESETS)


def test_first_party_compatibility_presets_are_explicit_legacy_fallbacks() -> None:
    anthropic = get_preset("anthropic-compat")
    gemini = get_preset("gemini-compat")

    assert anthropic is not None
    assert anthropic.protocol == "openai_compat"
    assert anthropic.base_url == "https://api.anthropic.com/v1/"
    assert anthropic.web_search_adapter == "anthropic_messages"
    assert gemini is not None
    assert gemini.protocol == "openai_compat"
    assert gemini.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert gemini.web_search_adapter == "gemini_grounding"


def test_provider_presets_select_documented_hosted_search_adapters() -> None:
    expected = {
        "openai": "openai_responses",
        "openai-responses": "openai_responses",
        "anthropic": "anthropic_messages",
        "anthropic-compat": "anthropic_messages",
        "anthropic-native": "anthropic_messages",
        "gemini": "gemini_grounding",
        "gemini-compat": "gemini_grounding",
        "gemini-native": "gemini_grounding",
        "qwen-intl": "dashscope_chat",
        "qwen-us": "dashscope_chat",
        "qwen-cn": "dashscope_chat",
        "zhipu": "zhipu_web_search",
        "moonshot": "moonshot_kimi",
        "moonshot-cn": "moonshot_kimi",
        "minimax": "minimax_coding_plan",
        "bytedance": "volcengine_web_search",
        "groq": "groq_compound",
        "mistral": "mistral_conversations",
        "xai": "xai_responses",
        "cohere": "cohere_web_search",
        "openrouter": "openrouter_web",
        "perplexity": "perplexity_sonar",
    }

    actual = {key: get_preset(key).web_search_adapter for key in expected}  # type: ignore[union-attr]

    assert actual == expected


def test_presets_without_provider_hosted_search_remain_model_independent() -> None:
    for key in (
        "deepseek",
        "zai-coding-plan",
        "cerebras",
        "together",
        "fireworks",
        "kimi-code",
        "xiaomi-mimo",
        "ollama",
        "lm-studio",
        "vllm",
        "custom",
    ):
        preset = get_preset(key)
        assert preset is not None
        assert preset.web_search_adapter == "auto"


def test_compatibility_presets_stay_openai_compatible() -> None:
    for preset in PROFILE_PRESETS:
        if preset.key in {
            "openai-responses",
            "anthropic",
            "anthropic-native",
            "gemini",
            "gemini-native",
        }:
            continue
        assert preset.protocol == "openai_compat", preset.key


def test_each_non_custom_preset_has_valid_base_url_and_suggested_models() -> None:
    url_pattern = re.compile(r"^https?://[^/].*$")
    for preset in PROFILE_PRESETS:
        if preset.key == "custom":
            continue
        assert url_pattern.match(preset.base_url), preset.key
        assert preset.suggested_models, preset.key


def test_openai_preset_uses_working_chat_completion_models() -> None:
    preset = get_preset("openai")

    assert preset is not None
    assert preset.base_url == "https://api.openai.com/v1"
    assert preset.suggested_models == (
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "gpt-5.6-luna",
        "gpt-5.3-codex",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
    )


def test_openai_preset_preserves_legacy_nano_alias_without_hiding_current_nano() -> None:
    preset = get_preset("openai")

    assert preset is not None
    assert canonical_model_alias_for_preset(preset, "gpt-5-nano") == "gpt-5.4-nano"
    assert canonical_model_alias_for_preset(preset, "gpt-5.4-nano") == "gpt-5.4-nano"


def test_moonshot_presets_alias_retired_kimi_k2_to_current_model() -> None:
    for key in ("moonshot", "moonshot-cn"):
        preset = get_preset(key)

        assert preset is not None
        assert "kimi-k2" not in preset.suggested_models
        assert canonical_model_alias_for_preset(preset, "kimi-k2") == "kimi-k2.6"


def test_provider_presets_use_current_openai_compatible_base_urls() -> None:
    expected_base_urls = {
        # The Alysis Code hosted proxy (llm Edge Function, DeepSeek upstream).
        "alysis": "https://vzigujbcjjmpntxhmyvr.supabase.co/functions/v1/llm/v1",
        "openai": "https://api.openai.com/v1",
        "openai-responses": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "anthropic-compat": "https://api.anthropic.com/v1/",
        "anthropic-native": "https://api.anthropic.com/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta",
        "gemini-compat": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-native": "https://generativelanguage.googleapis.com/v1beta",
        "deepseek": "https://api.deepseek.com",
        "nvidia": "https://integrate.api.nvidia.com/v1",
        "qwen-intl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "qwen-us": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        "qwen-cn": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4/",
        "zai-coding-plan": "https://api.z.ai/api/coding/paas/v4",
        "moonshot": "https://api.moonshot.ai/v1",
        "kimi-code": "https://api.kimi.com/coding/v1",
        "moonshot-cn": "https://api.moonshot.cn/v1",
        "minimax": "https://api.minimax.io/v1",
        "xiaomi-mimo": "https://api.xiaomimimo.com/v1",
        "bytedance": "https://ark.cn-beijing.volces.com/api/v3",
        "groq": "https://api.groq.com/openai/v1",
        "cerebras": "https://api.cerebras.ai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "xai": "https://api.x.ai/v1",
        "cohere": "https://api.cohere.ai/compatibility/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "perplexity": "https://api.perplexity.ai",
        "together": "https://api.together.ai/v1",
        "fireworks": "https://api.fireworks.ai/inference/v1",
        "ollama": "http://localhost:11434/v1",
        "lm-studio": "http://localhost:1234/v1",
        "vllm": "http://localhost:8000/v1",
        "custom": "",
    }

    actual_base_urls = {preset.key: preset.base_url for preset in PROFILE_PRESETS}

    assert actual_base_urls == expected_base_urls


def test_anthropic_preset_uses_native_messages_endpoint_and_current_models() -> None:
    preset = get_preset("anthropic")

    assert preset is not None
    assert preset.protocol == "anthropic_messages"
    assert preset.base_url == "https://api.anthropic.com/v1"
    assert preset.base_url.rstrip("/").endswith("/v1")
    assert preset.extra_headers == {}
    assert preset.suggested_models == (
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-fable-5",
        "claude-haiku-4-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
    )
    assert all(model in preset.suggested_model_descriptions for model in preset.suggested_models)


def test_anthropic_presets_all_offer_the_same_claude_roster() -> None:
    rosters = {
        key: get_preset(key).suggested_models  # type: ignore[union-attr]
        for key in ("anthropic", "anthropic-compat", "anthropic-native")
    }

    assert len(set(rosters.values())) == 1, rosters


def test_anthropic_cache_minimum_is_per_model_not_one_number() -> None:
    preset = get_preset("anthropic")
    assert preset is not None
    capability = preset.cache_capability
    assert capability is not None

    def minimum(model: str) -> int | None:
        return resolve_effective_cache_capability(
            provider_key="anthropic",
            protocol="anthropic_messages",
            model=model,
            transport_capabilities=None,
            preset_cache_capability=capability,
        ).min_cacheable_tokens

    # Opus 5 halved the floor; the older Opus tiers never did.
    assert minimum("claude-opus-5") == 512
    assert minimum("claude-fable-5") == 512
    assert minimum("claude-opus-4-8") == 1024
    assert minimum("claude-sonnet-5") == 1024
    assert minimum("claude-opus-4-7") == 2048
    assert minimum("claude-haiku-4-5") == 4096


def test_first_party_presets_do_not_default_to_preview_only_models() -> None:
    for key in (
        "openai",
        "openai-responses",
        "anthropic",
        "anthropic-compat",
        "gemini",
        "gemini-compat",
    ):
        preset = get_preset(key)
        assert preset is not None
        assert preset.suggested_models
        default_model = preset.suggested_models[0]
        assert "preview" not in default_model
        assert not default_model.endswith("-latest")


def test_launch_provider_presets_use_supported_chat_models() -> None:
    expected_models = {
        "deepseek": (
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        ),
        "gemini": (
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-pro-preview",
        ),
        "groq": (
            "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b",
            "openai/gpt-oss-20b",
            "groq/compound",
        ),
        "mistral": (
            "mistral-medium-3-5",
            "mistral-large-2512",
            "mistral-small-2603",
            "codestral-2508",
            "ministral-8b-2512",
        ),
        "moonshot": (
            "kimi-k2.7-code",
            "kimi-k3",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        ),
        "moonshot-cn": (
            "kimi-k2.7-code",
            "kimi-k3",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        ),
        "nvidia": (
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia/nemotron-3-nano-30b-a3b",
            "deepseek-ai/deepseek-v4-pro",
            "deepseek-ai/deepseek-v4-flash",
        ),
        "zai-coding-plan": ("glm-5.3", "glm-5-turbo", "glm-4.7"),
        "kimi-code": ("k3", "kimi-for-coding", "kimi-for-coding-highspeed"),
        "openrouter": (
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.8",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-luna",
            "z-ai/glm-5.2",
            "deepseek/deepseek-v4-pro-0813",
            "deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-v4-flash-vision-exp",
            "qwen/qwen3.8-max",
        ),
        "together": (
            "zai-org/GLM-5.2",
            "moonshotai/Kimi-K2.7-Code",
            "deepseek-ai/DeepSeek-V4-Pro-0813",
            "deepseek-ai/DeepSeek-V4-Flash-0731",
            "MiniMaxAI/MiniMax-M3",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
        ),
        "xai": (
            "grok-4.6",
            "grok-4.5",
            "grok-build-0.1",
            "grok-4.3",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
        ),
    }

    for provider, models in expected_models.items():
        preset = get_preset(provider)

        assert preset is not None
        assert preset.suggested_models == models
        assert all(model in preset.suggested_model_descriptions for model in models)


def test_moonshot_presets_keep_k2_web_search_and_legacy_alias() -> None:
    for key in ("moonshot", "moonshot-cn"):
        preset = get_preset(key)

        assert preset is not None
        assert preset.suggested_models[0] == "kimi-k2.7-code"
        assert preset.web_search_model == "kimi-k2.6"
        assert canonical_model_alias_for_preset(preset, "kimi-k2") == "kimi-k2.6"


def test_deepseek_preset_does_not_offer_legacy_or_retired_aliases() -> None:
    preset = get_preset("deepseek")

    assert preset is not None
    assert "deepseek-coder" not in preset.suggested_models
    assert "deepseek-chat" not in preset.suggested_models
    assert "deepseek-reasoner" not in preset.suggested_models


def test_nvidia_preset_uses_hosted_nim_contract() -> None:
    preset = get_preset("nvidia")

    assert preset is not None
    assert preset.provider_key == "nvidia"
    assert preset.protocol == "openai_compat"
    assert preset.api_key_env == "NVIDIA_API_KEY"
    assert preset.validation_model == "nvidia/nemotron-3-nano-30b-a3b"
    assert preset.web_search_adapter == "auto"
    assert "rate-limited" in preset.setup_warning


def test_zai_coding_plan_preset_is_separate_from_general_zhipu_api() -> None:
    preset = get_preset("zai-coding-plan")

    assert preset is not None
    assert preset.provider_key == "zai_coding_plan"
    assert preset.protocol == "openai_compat"
    assert preset.base_url == "https://api.z.ai/api/coding/paas/v4"
    assert preset.api_key_env == "ZAI_API_KEY"
    assert preset.validation_model == "glm-4.7"
    assert preset.model_aliases == {}
    assert "glm-5.2" not in preset.suggested_models
    assert "supported coding tools" in preset.setup_warning


def test_qwen_intl_offers_38_max_without_changing_the_balanced_default() -> None:
    preset = get_preset("qwen-intl")
    us_preset = get_preset("qwen-us")
    cn_preset = get_preset("qwen-cn")

    assert preset is not None
    assert us_preset is not None
    assert cn_preset is not None
    assert preset.suggested_models[:3] == (
        "qwen3.7-plus",
        "qwen3.8-max",
        "qwen3.7-max",
    )
    assert preset.suggested_model_descriptions["qwen3.8-max"].startswith("advanced")
    assert us_preset.suggested_models[:3] == preset.suggested_models[:3]
    assert cn_preset.suggested_models[:3] == preset.suggested_models[:3]


def test_fireworks_uses_current_dated_deepseek_v4_ids() -> None:
    preset = get_preset("fireworks")

    assert preset is not None
    assert "accounts/fireworks/models/deepseek-v4-pro-0813" in preset.suggested_models
    assert "accounts/fireworks/models/deepseek-v4-flash-0731" in preset.suggested_models
    assert "accounts/fireworks/models/deepseek-v4-pro" not in preset.suggested_models
    assert "accounts/fireworks/models/deepseek-v4-flash" not in preset.suggested_models
    assert preset.validation_model == "accounts/fireworks/models/deepseek-v4-flash-0731"


def test_gemini_aliases_preserve_active_and_provider_managed_model_ids() -> None:
    preset = get_preset("gemini")

    assert preset is not None
    for model in (
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
        "gemini-pro-latest",
    ):
        assert canonical_model_alias_for_preset(preset, model) == model

    assert canonical_model_alias_for_preset(preset, "gemini-2.0-flash") == ("gemini-3.6-flash")


def test_mistral_uses_documented_medium_id_and_preserves_legacy_default() -> None:
    preset = get_preset("mistral")

    assert preset is not None
    assert preset.suggested_models[0] == "mistral-medium-3-5"
    assert canonical_model_alias_for_preset(preset, "mistral-medium-2604") == ("mistral-medium-3-5")
    assert canonical_model_alias_for_preset(preset, "mistral-medium-latest") == (
        "mistral-medium-latest"
    )


def test_find_preset_for_profile_matches_base_url_without_requiring_env_var() -> None:
    profile = ProfileSpec(name="legacy", base_url="https://api.openai.com/v1")

    preset = find_preset_for_profile(profile)

    assert preset is not None
    assert preset.key == "openai"


def test_find_preset_for_profile_is_protocol_aware_for_native_variants() -> None:
    cases = [
        ("openai-responses", "openai_responses", "https://api.openai.com/v1"),
        ("anthropic", "anthropic_messages", "https://api.anthropic.com/v1"),
        ("anthropic-native", "anthropic_messages", "https://api.anthropic.com/v1"),
        (
            "gemini",
            "gemini_generate_content",
            "https://generativelanguage.googleapis.com/v1beta",
        ),
        (
            "gemini-native",
            "gemini_generate_content",
            "https://generativelanguage.googleapis.com/v1beta",
        ),
    ]

    for name, protocol, base_url in cases:
        preset = find_preset_for_profile(
            ProfileSpec(name=name, protocol=protocol, base_url=base_url)
        )

        assert preset is not None
        assert preset.key == name


def test_find_preset_for_profile_prefers_protocol_base_url_over_compatibility_base_match() -> None:
    profile = ProfileSpec(
        name="work-openai",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
    )

    preset = find_preset_for_profile(profile)

    assert preset is not None
    assert preset.key == "openai-responses"


def test_find_preset_for_profile_keeps_legacy_base_url_only_profiles_compatibility() -> None:
    anthropic = find_preset_for_profile(
        ProfileSpec(name="legacy-claude", base_url="https://api.anthropic.com/v1")
    )
    gemini = find_preset_for_profile(
        ProfileSpec(
            name="legacy-gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    )

    assert anthropic is not None
    assert anthropic.key == "anthropic-compat"
    assert gemini is not None
    assert gemini.key == "gemini-compat"


def test_profile_conversion_targets_first_party_native_and_compatibility_presets() -> None:
    profile = ProfileSpec(
        name="claude",
        protocol="openai_compat",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-6",
        web_search_adapter="anthropic_messages",
    )

    native_preset = target_preset_for_profile_conversion(profile, target="native")
    assert native_preset is not None
    assert native_preset.key == "anthropic"

    converted = convert_profile_to_preset(profile, native_preset)
    assert converted.name == "claude"
    assert converted.protocol == "anthropic_messages"
    assert converted.base_url == "https://api.anthropic.com/v1"
    # claude-sonnet-4-6 is a stale alias now; conversion canonicalises it.
    assert converted.default_model == "claude-sonnet-5"

    compat_preset = target_preset_for_profile_conversion(converted, target="compatibility")
    assert compat_preset is not None
    assert compat_preset.key == "anthropic-compat"


def test_profile_conversion_replaces_known_incompatible_model() -> None:
    profile = ProfileSpec(
        name="gemini",
        protocol="openai_compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        default_model="claude-sonnet-4-6",
        web_search_adapter="gemini_grounding",
    )

    native_preset = target_preset_for_profile_conversion(profile, target="native")
    assert native_preset is not None

    converted = convert_profile_to_preset(profile, native_preset)

    assert converted.protocol == "gemini_generate_content"
    assert converted.default_model == "gemini-3.7-flash"


def test_profile_conversion_replaces_provider_qualified_model_ids() -> None:
    profile = ProfileSpec(
        name="anthropic",
        protocol="openai_compat",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="anthropic/claude-sonnet-4-6",
        web_search_adapter="anthropic_messages",
    )

    native_preset = target_preset_for_profile_conversion(profile, target="native")
    assert native_preset is not None

    converted = convert_profile_to_preset(profile, native_preset)

    assert converted.protocol == "anthropic_messages"
    assert converted.default_model == "claude-sonnet-5"


def test_profile_conversion_replaces_known_stale_alias_for_target_preset() -> None:
    profile = ProfileSpec(
        name="gemini",
        protocol="openai_compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        # gemini-2.0-flash is shut down and aliases to Google's documented
        # replacement; active stable and provider-managed aliases are retained.
        default_model="gemini-2.0-flash",
        web_search_adapter="gemini_grounding",
    )

    native_preset = target_preset_for_profile_conversion(profile, target="native")
    assert native_preset is not None

    converted = convert_profile_to_preset(profile, native_preset)

    assert converted.protocol == "gemini_generate_content"
    assert converted.default_model == "gemini-3.6-flash"


def test_find_preset_for_base_url_matches_known_provider() -> None:
    preset = find_preset_for_base_url("https://api.anthropic.com/v1/")

    assert preset is not None
    assert preset.key == "anthropic-compat"


def test_custom_preset_has_empty_base_url() -> None:
    preset = get_preset("custom")

    assert preset is not None
    assert preset.base_url == ""


def test_make_profile_from_preset_uses_preset_key_as_name_default() -> None:
    preset = get_preset("openai")

    assert preset is not None
    assert make_profile_from_preset(preset).name == "openai"


def test_xiaomi_mimo_is_a_plain_byok_preset() -> None:
    preset = get_preset("xiaomi-mimo")

    assert preset is not None
    assert preset.label == "Xiaomi MiMo"
    assert preset.protocol == "openai_compat"
    assert preset.base_url == "https://api.xiaomimimo.com/v1"
    assert preset.api_key_env == "XIAOMI_API_KEY"
    assert preset.suggested_models == ("mimo-v2.5-pro", "mimo-v2-flash", "mimo-v2.5")
    assert all(model in preset.suggested_model_descriptions for model in preset.suggested_models)
    assert preset.validation_model == "mimo-v2.5-pro"
    # The legacy bare "mimo" placeholder migrates to the flagship model.
    assert canonical_model_alias_for_preset(preset, "mimo") == "mimo-v2.5-pro"
    # No special label casing: the picker shows the plain preset label.
    assert preset_selection_label(preset) == "Xiaomi MiMo"


def test_primary_picker_lists_xiaomi_mimo_in_registration_order_without_alysis() -> None:
    keys = [preset.key for preset in provider_selection_presets()]

    assert "alysis" not in keys
    assert "xiaomi-mimo" in keys
    # Registration order among the other hosted vendors — no special sorting,
    # not a headline choice, not sorted last.
    assert keys.index("minimax") < keys.index("xiaomi-mimo") < keys.index("bytedance")
    assert keys[0] != "xiaomi-mimo"
    assert keys[-1] != "xiaomi-mimo"


def test_primary_picker_never_carries_the_alysis_brand_as_a_provider() -> None:
    for preset in provider_selection_presets():
        assert "alysis" not in preset.label.casefold(), preset.key
        assert "alysis" not in preset_selection_label(preset).casefold(), preset.key


def test_hosted_alysis_preset_stays_intact_behind_the_advanced_picker() -> None:
    advanced_keys = [preset.key for preset in advanced_provider_selection_presets()]
    assert "alysis" in advanced_keys

    hosted = get_preset("alysis")
    assert hosted is not None
    assert preset_selection_label(hosted) == "Alysis Code Pro (hosted models) - requires login"
    # The preset itself must keep working for `alysis login` / alysis_cloud.
    assert hosted.api_key_env is None
    # Retired MiMo-trial ids migrate to the Pro default.
    assert canonical_model_alias_for_preset(hosted, "mimo") == "deepseek-v4-flash"
