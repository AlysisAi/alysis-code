from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .branding import env_get
from .chatgpt_codex_static_provider import (
    CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE,
    resolve_chatgpt_codex_static_model,
)
from .config import AppConfig
from .litellm_static_provider import (
    BUNDLED_MODEL_CATALOG_SOURCE,
    resolve_litellm_static_metadata,
)
from .model_metadata_utils import (
    model_name_variants,
    normalize_base_url,
    parse_bool,
    parse_non_negative_float,
    parse_positive_int,
)
from .provider_url import known_provider_key_from_base_url

_INT_FIELDS = ("context_window_tokens", "max_output_tokens")
_FLOAT_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_cost_per_token",
    "cache_creation_input_cost_per_token",
    "cache_creation_5m_input_cost_per_token",
    "cache_creation_1h_input_cost_per_token",
    "reasoning_output_cost_per_token",
)
_BOOL_FIELDS = ("supports_vision", "supports_reasoning")
_TRACKED_FIELDS = (
    "context_window_tokens",
    "max_output_tokens",
    "supports_vision",
    "supports_reasoning",
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_cost_per_token",
    "cache_creation_input_cost_per_token",
    "cache_creation_5m_input_cost_per_token",
    "cache_creation_1h_input_cost_per_token",
    "reasoning_output_cost_per_token",
)
DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS = 8_192

_FALLBACKS: dict[str, Any] = {
    "context_window_tokens": DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS,
    "max_output_tokens": DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
    "supports_vision": False,
    "supports_reasoning": None,
    "input_cost_per_token": None,
    "output_cost_per_token": None,
    "cache_read_input_cost_per_token": None,
    "cache_creation_input_cost_per_token": None,
    "cache_creation_5m_input_cost_per_token": None,
    "cache_creation_1h_input_cost_per_token": None,
    "reasoning_output_cost_per_token": None,
}
_ENV_FIELD_MAP: dict[str, str] = {
    "context_window_tokens": "ALYSIS_CONTEXT_WINDOW",
    "max_output_tokens": "ALYSIS_MAX_OUTPUT_TOKENS",
    "supports_vision": "ALYSIS_SUPPORTS_VISION",
    "supports_reasoning": "ALYSIS_SUPPORTS_REASONING",
    "input_cost_per_token": "ALYSIS_INPUT_COST_PER_TOKEN",
    "output_cost_per_token": "ALYSIS_OUTPUT_COST_PER_TOKEN",
    "cache_read_input_cost_per_token": "ALYSIS_CACHE_READ_INPUT_COST_PER_TOKEN",
    "cache_creation_input_cost_per_token": "ALYSIS_CACHE_CREATION_INPUT_COST_PER_TOKEN",
    "cache_creation_5m_input_cost_per_token": ("ALYSIS_CACHE_CREATION_5M_INPUT_COST_PER_TOKEN"),
    "cache_creation_1h_input_cost_per_token": ("ALYSIS_CACHE_CREATION_1H_INPUT_COST_PER_TOKEN"),
    "reasoning_output_cost_per_token": "ALYSIS_REASONING_OUTPUT_COST_PER_TOKEN",
}
_DEPRECATED_MODEL_CAPABILITIES_WARNING = (
    "Config key `model_capabilities` is deprecated and ignored; use `model_metadata_overrides`."
)
_FALLBACK_WARNING = (
    "Using fallback context/max_output; set model_metadata_overrides for best performance."
)
OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE = "official_provider_model_catalog"
CANONICAL_MODEL_CATALOG_SOURCE = "canonical_model_catalog"
_CANONICAL_MODEL_SOURCES: dict[str, tuple[str, ...]] = {
    "qwen3.8-max": ("https://help.aliyun.com/en/model-studio/qwen3-8-max",),
    "deepseek-v4-pro": ("https://api-docs.deepseek.com/quick_start/pricing/",),
    "deepseek-v4-flash": ("https://api-docs.deepseek.com/quick_start/pricing/",),
    "deepseek-v4-flash-vision-exp": ("https://api-docs.deepseek.com/quick_start/pricing/",),
}
_CANONICAL_MODEL_METADATA: dict[str, dict[str, Any]] = {
    "qwen3.8-max": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "deepseek-v4-pro": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "supports_reasoning": True,
    },
    "deepseek-v4-flash": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "supports_reasoning": True,
    },
    "deepseek-v4-flash-vision-exp": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": True,
        "supports_reasoning": True,
    },
}
# Provider model ids are routing details, not separate capability records. This
# declarative alias table lets each route inherit the canonical model's limits
# while retaining provider-specific pricing, availability, and stricter caps.
_PROVIDER_CANONICAL_MODEL_IDS: dict[str, dict[str, str]] = {
    "qwen": {
        "qwen3.8-max": "qwen3.8-max",
    },
    "deepseek": {
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp": "deepseek-v4-flash-vision-exp",
    },
    "openrouter": {
        "qwen/qwen3.8-max": "qwen3.8-max",
        "deepseek/deepseek-v4-pro-0813": "deepseek-v4-pro",
        "deepseek/deepseek-v4-flash-0731": "deepseek-v4-flash",
        "deepseek/deepseek-v4-flash-vision-exp": "deepseek-v4-flash-vision-exp",
    },
    "together": {
        "deepseek-ai/DeepSeek-V4-Pro-0813": "deepseek-v4-pro",
        "deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek-v4-flash",
    },
    "fireworks": {
        "accounts/fireworks/models/deepseek-v4-pro-0813": "deepseek-v4-pro",
        "accounts/fireworks/models/deepseek-v4-flash-0731": "deepseek-v4-flash",
    },
    "nvidia": {
        "deepseek-ai/deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-ai/deepseek-v4-flash": "deepseek-v4-flash",
    },
}
_OFFICIAL_PROVIDER_MODEL_SOURCES: dict[str, dict[str, tuple[str, ...]]] = {
    "zai_coding_plan": {
        "glm-5.3": ("https://docs.z.ai/guides/llm/glm-5.3",),
        "glm-5-turbo": ("https://docs.z.ai/guides/llm/glm-5-turbo",),
        "glm-4.7": ("https://docs.z.ai/guides/llm/glm-4.7",),
    },
    "xai": {
        "grok-4.6": (
            "https://docs.x.ai/developers/grok-4-6",
            "https://docs.x.ai/developers/pricing",
        ),
    },
    "gemini": {
        model: (model_url, "https://ai.google.dev/gemini-api/docs/pricing")
        for model, model_url in {
            "gemini-3.7-flash": ("https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash"),
            "gemini-3.6-flash": ("https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash"),
            "gemini-3.5-flash-lite": (
                "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite"
            ),
        }.items()
    },
    "qwen": {
        "qwen3.8-max": (
            "https://help.aliyun.com/en/model-studio/qwen3-8-max",
            "https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions",
        ),
    },
    "deepseek": {
        model: (
            "https://api-docs.deepseek.com/quick_start/pricing/",
            "https://api-docs.deepseek.com/guides/thinking_mode/",
        )
        for model in (
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        )
    },
    "openrouter": {
        model: ("https://openrouter.ai/api/v1/models",)
        for model in (
            "qwen/qwen3.8-max",
            "deepseek/deepseek-v4-pro-0813",
            "deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-v4-flash-vision-exp",
        )
    },
    "together": {
        model: ("https://docs.together.ai/docs/serverless/models",)
        for model in (
            "deepseek-ai/DeepSeek-V4-Pro-0813",
            "deepseek-ai/DeepSeek-V4-Flash-0731",
        )
    },
    "fireworks": {
        "accounts/fireworks/models/deepseek-v4-pro-0813": (
            "https://fireworks.ai/models/deepseek-ai/deepseek-v4-pro-0813",
        ),
        "accounts/fireworks/models/deepseek-v4-flash-0731": (
            "https://fireworks.ai/models/deepseek-ai/deepseek-v4-flash-0731",
        ),
    },
    "nvidia": {
        "nvidia/nemotron-3-super-120b-a12b": (
            "https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/build",
        ),
        "nvidia/nemotron-3-ultra-550b-a55b": (
            "https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b",
        ),
        "nvidia/nemotron-3-nano-30b-a3b": (
            "https://build.nvidia.com/nvidia/nemotron-3-nano-30b-a3b",
        ),
        "deepseek-ai/deepseek-v4-pro": ("https://build.nvidia.com/deepseek-ai/deepseek-v4-pro",),
        "deepseek-ai/deepseek-v4-flash": (
            "https://build.nvidia.com/deepseek-ai/deepseek-v4-flash",
        ),
    },
    "moonshot": {
        model: ("https://platform.moonshot.ai/docs/pricing/chat",)
        for model in (
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        )
    },
    "anthropic": {
        "claude-opus-5": (
            "https://docs.anthropic.com/en/docs/about-claude/models/overview",
            "https://docs.anthropic.com/en/docs/about-claude/pricing",
        ),
    },
}
_OFFICIAL_PROVIDER_MODEL_METADATA: dict[str, dict[str, dict[str, Any]]] = {
    "zai_coding_plan": {
        # GLM-5.3 is currently exclusive to the subscription Coding Plan API;
        # the general pay-per-token API is still marked "coming soon". Plan
        # credits are not token prices, so this route intentionally carries no
        # monetary cost fields.
        "glm-5.3": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "glm-5-turbo": {
            "context_window_tokens": 200_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "glm-4.7": {
            "context_window_tokens": 200_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
        },
    },
    "xai": {
        # Grok 4.6 postdates the provenance-pinned LiteLLM snapshot. xAI
        # publishes a 500K shared context with no separate text-output limit,
        # so use the full window here and let the registry's generic
        # shared-window policy reserve a practical output allowance. These are
        # the base rates for prompts up to 200K tokens; xAI doubles all three
        # rates above that threshold, which this flat-price schema cannot
        # represent yet.
        "grok-4.6": {
            "context_window_tokens": 500_000,
            "max_output_tokens": 500_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.0000005,
            "reasoning_output_cost_per_token": 0.000006,
        },
    },
    "gemini": {
        # These GA models postdate the provenance-pinned LiteLLM snapshot.
        # Capacity and current standard-tier pricing are from Google's model
        # and pricing pages. The 3.7/3.6 introductory rates expire 2026-12-31.
        "gemini-3.7-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000075,
            "output_cost_per_token": 0.00000375,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.00000375,
        },
        "gemini-3.6-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000075,
            "output_cost_per_token": 0.00000375,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.00000375,
        },
        "gemini-3.5-flash-lite": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000003,
            "output_cost_per_token": 0.0000025,
            "cache_read_input_cost_per_token": 0.00000003,
            "reasoning_output_cost_per_token": 0.0000025,
        },
    },
    "qwen": {
        # Qwen3.8-Max is available from the China, Singapore, Frankfurt, US,
        # and Tokyo surfaces under the same bare model id. Prices vary by
        # region/currency, which this provider-level flat schema cannot encode,
        # so costs remain unknown instead of applying one region globally.
        "qwen3.8-max": {},
    },
    "deepseek": {
        # DeepSeek charges half price outside two daily peak windows. The
        # registry cannot express time-dependent rates, so use peak pricing as
        # a conservative upper-bound estimate rather than under-reporting cost.
        "deepseek-v4-pro": {
            "input_cost_per_token": 0.00000132,
            "output_cost_per_token": 0.00000396,
            "cache_read_input_cost_per_token": 0.000000044,
            "reasoning_output_cost_per_token": 0.00000396,
        },
        "deepseek-v4-flash": {
            "input_cost_per_token": 0.00000044,
            "output_cost_per_token": 0.00000132,
            "cache_read_input_cost_per_token": 0.000000014,
            "reasoning_output_cost_per_token": 0.00000132,
        },
        "deepseek-v4-flash-vision-exp": {
            "input_cost_per_token": 0.00000044,
            "output_cost_per_token": 0.00000132,
            "cache_read_input_cost_per_token": 0.000000014,
            "reasoning_output_cost_per_token": 0.00000132,
        },
    },
    "openrouter": {
        "qwen/qwen3.8-max": {
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.00000025,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.000006,
        },
        "deepseek/deepseek-v4-pro-0813": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.000001188,
            "output_cost_per_token": 0.000003564,
            "cache_read_input_cost_per_token": 0.0000000396,
            "reasoning_output_cost_per_token": 0.000003564,
        },
        "deepseek/deepseek-v4-flash-0731": {
            # OpenRouter's public model catalog advertises a 1.31M routing
            # window for this snapshot even though its current top provider is
            # capped at 1,048,576 tokens. Preserve the gateway-level contract;
            # route-specific limits remain OpenRouter's responsibility.
            "context_window_tokens": 1_310_720,
            "input_cost_per_token": 0.00000008,
            "output_cost_per_token": 0.00000018,
            "cache_read_input_cost_per_token": 0.000000016,
            "reasoning_output_cost_per_token": 0.00000018,
        },
        "deepseek/deepseek-v4-flash-vision-exp": {
            "context_window_tokens": 1_048_576,
            # OpenRouter exposes DeepSeek's time-window overrides. Use the
            # highest published rate so a flat estimate does not undercount.
            "input_cost_per_token": 0.00000044,
            "output_cost_per_token": 0.00000132,
            "cache_read_input_cost_per_token": 0.000000014,
            "reasoning_output_cost_per_token": 0.00000132,
        },
    },
    "together": {
        "deepseek-ai/DeepSeek-V4-Pro-0813": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000132,
            "output_cost_per_token": 0.00000396,
            "cache_read_input_cost_per_token": 0.00000013,
            "reasoning_output_cost_per_token": 0.00000396,
        },
        "deepseek-ai/DeepSeek-V4-Flash-0731": {
            "input_cost_per_token": 0.00000014,
            "output_cost_per_token": 0.00000028,
            "cache_read_input_cost_per_token": 0.00000003,
            "reasoning_output_cost_per_token": 0.00000028,
        },
    },
    "fireworks": {
        "accounts/fireworks/models/deepseek-v4-pro-0813": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000132,
            "output_cost_per_token": 0.00000396,
            "cache_read_input_cost_per_token": 0.000000044,
            "reasoning_output_cost_per_token": 0.00000396,
        },
        "accounts/fireworks/models/deepseek-v4-flash-0731": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000014,
            "output_cost_per_token": 0.00000028,
            "cache_read_input_cost_per_token": 0.000000028,
            "reasoning_output_cost_per_token": 0.00000028,
        },
    },
    "nvidia": {
        # NVIDIA's hosted NIM catalog publishes the Nemotron 3 Super and Ultra
        # models with 1M context. Nano's hosted endpoint currently advertises
        # 262K. The hosted Nemotron endpoints accept up to 32,768 output tokens;
        # 16,384 is their default, not their ceiling. NVIDIA-hosted DeepSeek V4
        # uses a separate 16,384-token ceiling. Free Endpoint access is
        # rate-limited prototyping, so no durable token price is encoded here.
        "nvidia/nemotron-3-super-120b-a12b": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 32_768,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "nvidia/nemotron-3-ultra-550b-a55b": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 32_768,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "nvidia/nemotron-3-nano-30b-a3b": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "deepseek-ai/deepseek-v4-pro": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
        },
        "deepseek-ai/deepseek-v4-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
        },
    },
    "moonshot": {
        "kimi-k3": {
            "context_window_tokens": 1_048_576,
            # K3 can use the full context for output, but 131,072 is the API's
            # normal completion allowance and therefore the useful local reserve.
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_cost_per_token": 0.0000003,
        },
        "kimi-k2.7-code": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000095,
            "output_cost_per_token": 0.000004,
            "cache_read_input_cost_per_token": 0.00000019,
        },
        "kimi-k2.7-code-highspeed": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000019,
            "output_cost_per_token": 0.000008,
            "cache_read_input_cost_per_token": 0.00000038,
        },
        "kimi-k2.6": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000095,
            "output_cost_per_token": 0.000004,
            "cache_read_input_cost_per_token": 0.00000016,
        },
    },
    "anthropic": {
        # Opus 5 ships ahead of the vendored litellm mirror, and that mirror is
        # refresh-policy pinned to an upstream commit — so its capacity lives
        # here rather than as a hand-edit that would falsify the snapshot's
        # provenance. Drop this entry once a catalog refresh carries the model.
        # Same shape as its siblings: 1M input + 128K output, Opus 4.8 pricing.
        "claude-opus-5": {
            "context_window_tokens": 1_128_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
            "cache_read_input_cost_per_token": 0.0000005,
            "cache_creation_input_cost_per_token": 0.00000625,
            "cache_creation_5m_input_cost_per_token": 0.00000625,
            "cache_creation_1h_input_cost_per_token": 0.00001,
        },
    },
}
_BUILT_IN_MODEL_METADATA: dict[str, dict[str, Any]] = {
    "deepseek-chat": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
    },
    "deepseek-reasoner": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
    },
    # Hosted Xiaomi MiMo trial models. The proxy serves these ids when allowlisted,
    # and falls back server-side to its canonical MiMo model otherwise.
    "mimo-v2.5-pro": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": False,
        "input_cost_per_token": 0.000001,
        "output_cost_per_token": 0.000003,
    },
    "mimo-v2-flash": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 65_536,
        "supports_vision": False,
        "input_cost_per_token": 0.0000001,
        "output_cost_per_token": 0.0000003,
    },
    "mimo-v2.5": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "input_cost_per_token": 0.0000004,
        "output_cost_per_token": 0.000002,
    },
    # Legacy friendly id kept for sessions that logged in before model choice.
    # Hosted Xiaomi MiMo trial: the CLI sends the friendly id "mimo" (the proxy
    # pins it to the real upstream id server-side). Without an entry here the id
    # resolves nowhere and falls back to generic unknown-model metadata, emitting
    # a metadata note on every run and silently shrinking the usable context.
    # Values mirror the bundled `openrouter/xiaomi/mimo-v2-flash` catalog entry
    # (262144 input / 16384 output); costs use the mimo-v2.5-pro list price.
    "mimo": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 16_384,
        "supports_vision": False,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
    },
    # Kimi Code membership ids served by api.kimi.com/coding/v1 (subscription
    # billing, so no per-token costs). Context windows from
    # kimi.com/code/docs, July 2026.
    "k3": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 1_048_576,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "kimi-for-coding": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "kimi-for-coding-highspeed": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    # Moonshot Kimi models newer than the bundled LiteLLM snapshot (which tops
    # out at moonshot/kimi-k2.6). Values from platform.kimi.ai/docs/pricing,
    # July 2026; remove once a snapshot refresh covers these ids.
    "kimi-k3": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 1_048_576,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_read_input_cost_per_token": 0.0000003,
    },
    "kimi-k2.7-code": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.00000095,
        "output_cost_per_token": 0.000004,
        "cache_read_input_cost_per_token": 0.00000019,
    },
    "kimi-k2.7-code-highspeed": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.0000019,
        "output_cost_per_token": 0.000008,
        "cache_read_input_cost_per_token": 0.00000038,
    },
}


@dataclass(frozen=True)
class ModelMeta:
    model_name: str
    context_window_tokens: int
    max_output_tokens: int
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    cache_read_input_cost_per_token: float | None = None
    cache_creation_input_cost_per_token: float | None = None
    cache_creation_5m_input_cost_per_token: float | None = None
    cache_creation_1h_input_cost_per_token: float | None = None
    reasoning_output_cost_per_token: float | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "fallback"
    supports_vision: bool = False
    supports_reasoning: bool | None = None
    field_sources: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    provider_key: str | None = None


@dataclass
class _LayerData:
    name: str
    values: dict[str, Any] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    model_name: str | None = None


def _parse_field(field: str, raw_value: Any) -> Any | None:
    if field in _INT_FIELDS:
        return parse_positive_int(raw_value)
    if field in _FLOAT_FIELDS:
        return parse_non_negative_float(raw_value)
    if field in _BOOL_FIELDS:
        return parse_bool(raw_value)
    return None


def _dedupe_warnings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        if not clean:
            continue
        if clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _build_model_alias_index(mapping: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key in mapping:
        if not isinstance(key, str):
            continue
        for variant in model_name_variants(key):
            index.setdefault(variant.casefold(), key)
    return index


def _lookup_model_override(
    *,
    mapping: Any,
    requested_model: str,
    label: str,
    warnings: list[str],
) -> tuple[str, dict[str, Any]] | None:
    if mapping is None:
        return None
    if not isinstance(mapping, dict):
        warnings.append(f"Ignoring invalid {label}: expected object.")
        return None

    alias_index = _build_model_alias_index(mapping)
    for variant in model_name_variants(requested_model):
        key = alias_index.get(variant.casefold())
        if key is None:
            continue
        raw = mapping.get(key)
        if isinstance(raw, dict):
            return key, raw
        warnings.append(f"Ignoring invalid {label}[{key!r}]: expected object.")
        return None
    return None


def _lookup_endpoint_override(
    *,
    endpoints: Any,
    base_url: str,
    warnings: list[str],
) -> tuple[str, dict[str, Any]] | None:
    if endpoints is None:
        return None
    if not isinstance(endpoints, dict):
        warnings.append("Ignoring invalid model_metadata_overrides.endpoints: expected object.")
        return None

    if base_url in endpoints:
        exact = endpoints.get(base_url)
        if isinstance(exact, dict):
            return base_url, exact
        warnings.append(
            f"Ignoring invalid model_metadata_overrides.endpoints[{base_url!r}]: expected object."
        )
        return None

    normalized_base_url = normalize_base_url(base_url)
    if not normalized_base_url:
        return None
    for endpoint_key, endpoint_value in endpoints.items():
        if not isinstance(endpoint_key, str):
            continue
        if normalize_base_url(endpoint_key) != normalized_base_url:
            continue
        if isinstance(endpoint_value, dict):
            return endpoint_key, endpoint_value
        warnings.append(
            "Ignoring invalid model_metadata_overrides.endpoints"
            f"[{endpoint_key!r}]: expected object."
        )
        return None
    return None


class ModelRegistry:
    def __init__(self, *, cfg: AppConfig, api_key: str | None = None) -> None:
        _ = api_key
        self._cfg = cfg
        self.last_error: str | None = None
        self.last_warnings: list[str] = []
        self.last_source: str | None = None

    def _provider_route_identity(self) -> tuple[str | None, str]:
        """Resolve explicit preset route identity without trusting model aliases."""

        try:
            from .profile_presets import find_preset_for_profile
            from .profiles import get_active_profile

            profile = get_active_profile(self._cfg)
            preset = find_preset_for_profile(profile)
        except Exception:  # noqa: BLE001 - metadata fallback must remain offline-safe
            return None, str(self._cfg.base_url or "").strip()
        provider_key = str(preset.provider_key or "").strip() if preset is not None else ""
        base_url = str(profile.base_url or self._cfg.base_url or "").strip()
        return provider_key or None, base_url

    def _provider_hint(self) -> str | None:
        provider_key, _base_url = self._provider_route_identity()
        return provider_key

    def _resolve_env_layer(self) -> _LayerData:
        layer = _LayerData(name="env")
        for field_name, env_name in _ENV_FIELD_MAP.items():
            raw_value = env_get(env_name)
            if raw_value is None:
                continue
            parsed = _parse_field(field_name, raw_value)
            if parsed is None:
                layer.warnings.append(f"Ignoring invalid {env_name}: {raw_value!r}")
                continue
            layer.values[field_name] = parsed
            layer.field_sources[field_name] = f"env:{env_name}"
        return layer

    def _apply_user_scope(
        self,
        *,
        layer: _LayerData,
        source: str,
        payload: dict[str, Any],
    ) -> None:
        for field_name in _TRACKED_FIELDS:
            if field_name in layer.values:
                continue
            if field_name not in payload:
                continue
            raw_value = payload.get(field_name)
            if raw_value is None:
                continue
            parsed = _parse_field(field_name, raw_value)
            if parsed is None:
                layer.warnings.append(f"Ignoring invalid {field_name} in {source}: {raw_value!r}")
                continue
            layer.values[field_name] = parsed
            layer.field_sources[field_name] = source

    def _resolve_user_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name="user")
        raw_overrides = self._cfg.extra_fields.get("model_metadata_overrides")
        if raw_overrides is None:
            return layer
        if not isinstance(raw_overrides, dict):
            layer.warnings.append("Ignoring invalid model_metadata_overrides: expected object.")
            return layer

        scoped_payloads: list[tuple[str, dict[str, Any]]] = []
        endpoint_match = _lookup_endpoint_override(
            endpoints=raw_overrides.get("endpoints"),
            base_url=self._cfg.base_url,
            warnings=layer.warnings,
        )
        if endpoint_match is not None:
            endpoint_key, endpoint_payload = endpoint_match
            endpoint_model_match = _lookup_model_override(
                mapping=endpoint_payload.get("models"),
                requested_model=requested_model,
                label=f"model_metadata_overrides.endpoints[{endpoint_key!r}].models",
                warnings=layer.warnings,
            )
            if endpoint_model_match is not None:
                model_key, model_payload = endpoint_model_match
                scoped_payloads.append(
                    (
                        f"user:endpoints[{endpoint_key!r}].models[{model_key!r}]",
                        model_payload,
                    )
                )

            endpoint_default = endpoint_payload.get("default")
            if endpoint_default is not None:
                if isinstance(endpoint_default, dict):
                    scoped_payloads.append(
                        (f"user:endpoints[{endpoint_key!r}].default", endpoint_default)
                    )
                else:
                    layer.warnings.append(
                        "Ignoring invalid model_metadata_overrides.endpoints"
                        f"[{endpoint_key!r}].default: expected object."
                    )

        model_match = _lookup_model_override(
            mapping=raw_overrides.get("models"),
            requested_model=requested_model,
            label="model_metadata_overrides.models",
            warnings=layer.warnings,
        )
        if model_match is not None:
            model_key, model_payload = model_match
            scoped_payloads.append((f"user:models[{model_key!r}]", model_payload))

        default_payload = raw_overrides.get("default")
        if default_payload is not None:
            if isinstance(default_payload, dict):
                scoped_payloads.append(("user:default", default_payload))
            else:
                layer.warnings.append(
                    "Ignoring invalid model_metadata_overrides.default: expected object."
                )

        for source, payload in scoped_payloads:
            self._apply_user_scope(layer=layer, source=source, payload=payload)

        return layer

    def _resolve_bundled_model_catalog_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name=BUNDLED_MODEL_CATALOG_SOURCE)
        provider_hint, route_base_url = self._provider_route_identity()
        try:
            static_meta = resolve_litellm_static_metadata(
                requested_model,
                base_url=route_base_url,
                provider_hint=provider_hint,
            )
        except TypeError as exc:
            # Preserve compatibility with lightweight test/plugin resolvers that
            # implement the older ``(model, *, base_url)`` extension surface.
            if "provider_hint" not in str(exc):
                raise
            static_meta = resolve_litellm_static_metadata(
                requested_model,
                base_url=route_base_url,
            )
        layer.error = static_meta.error
        layer.raw_metadata = static_meta.raw_metadata
        layer.model_name = static_meta.model_key

        values = {
            "context_window_tokens": static_meta.context_window_tokens,
            "max_output_tokens": static_meta.max_output_tokens,
            "supports_vision": static_meta.supports_vision,
            "supports_reasoning": parse_bool(static_meta.raw_metadata.get("supports_reasoning")),
            "input_cost_per_token": static_meta.input_cost_per_token,
            "output_cost_per_token": static_meta.output_cost_per_token,
            "cache_read_input_cost_per_token": static_meta.cache_read_input_cost_per_token,
            "cache_creation_input_cost_per_token": (
                static_meta.cache_creation_input_cost_per_token
            ),
            "cache_creation_5m_input_cost_per_token": (
                static_meta.cache_creation_5m_input_cost_per_token
            ),
            "cache_creation_1h_input_cost_per_token": (
                static_meta.cache_creation_1h_input_cost_per_token
            ),
            "reasoning_output_cost_per_token": static_meta.reasoning_output_cost_per_token,
        }
        for field_name, value in values.items():
            if value is None:
                continue
            layer.values[field_name] = value
            layer.field_sources[field_name] = BUNDLED_MODEL_CATALOG_SOURCE
        return layer

    def _resolve_provider_auth_layer(self, requested_model: str) -> _LayerData:
        """Project the active account's live model catalog into context metadata."""

        layer = _LayerData(name="provider_auth")
        try:
            from .profiles import get_active_profile

            profile = get_active_profile(self._cfg)
        except Exception:  # noqa: BLE001 - malformed profiles fall through to static metadata
            return layer
        provider_id = str(profile.auth_provider or "").strip()
        if not provider_id:
            return layer
        try:
            from .provider_auth import create_provider_auth

            models = create_provider_auth(provider_id).list_models(refresh=False)
        except Exception as exc:  # noqa: BLE001 - offline startup must remain possible
            layer.warnings.append(f"Subscription model metadata unavailable: {exc}")
            models = ()

        requested_variants = {
            value.casefold() for value in model_name_variants(requested_model) if value
        }
        selected = next(
            (
                model
                for model in models
                if requested_variants.intersection(
                    value.casefold() for value in model_name_variants(model.id) if value
                )
            ),
            None,
        )
        if selected is None and provider_id != "openai-codex":
            return layer

        source = f"provider_auth:{provider_id}"
        static_subscription_model = (
            resolve_chatgpt_codex_static_model(requested_model)
            if provider_id == "openai-codex"
            else None
        )
        if selected is not None:
            layer.model_name = selected.id
        elif static_subscription_model is not None:
            layer.model_name = static_subscription_model.id
            source = f"provider_auth:{provider_id}:{CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE}"
            layer.warnings.append(
                "Using the bundled ChatGPT subscription capacity snapshot because live "
                "metadata for the selected model is unavailable."
            )
        else:
            layer.model_name = requested_model
            source = f"provider_auth:{provider_id}:conservative-default"
            layer.warnings.append(
                "Using conservative ChatGPT subscription capacity because the selected "
                "model is absent from both live and bundled subscription metadata."
            )

        context_window_tokens = (
            selected.context_window_tokens if selected is not None else None
        ) or (
            static_subscription_model.context_window_tokens
            if static_subscription_model is not None
            else None
        )
        max_output_tokens = (selected.max_output_tokens if selected is not None else None) or (
            static_subscription_model.max_output_tokens
            if static_subscription_model is not None
            else None
        )
        input_modalities = (
            selected.input_modalities
            if selected is not None
            else (
                static_subscription_model.input_modalities
                if static_subscription_model is not None
                else ("text",)
            )
        )
        layer.raw_metadata = {
            "provider_auth": provider_id,
            "subscription_backed": True,
            "input_modalities": list(input_modalities),
        }
        selected_reasoning_efforts = (
            selected.reasoning_efforts
            if selected is not None
            else (
                static_subscription_model.reasoning_efforts
                if static_subscription_model is not None
                else ()
            )
        )
        selected_default_effort = (
            selected.default_reasoning_effort
            if selected is not None
            else (
                static_subscription_model.default_reasoning_effort
                if static_subscription_model is not None
                else None
            )
        )
        if selected is not None or static_subscription_model is not None:
            supports_reasoning = bool(selected_reasoning_efforts or selected_default_effort)
            layer.values["supports_reasoning"] = supports_reasoning
            layer.field_sources["supports_reasoning"] = source
            layer.raw_metadata["supports_reasoning"] = supports_reasoning
        if context_window_tokens is not None:
            layer.values["context_window_tokens"] = context_window_tokens
            layer.field_sources["context_window_tokens"] = source
            layer.raw_metadata["context_window_tokens"] = context_window_tokens
            # The Codex catalog currently omits a response-output ceiling. Keep
            # Alysis Code's existing local reserve without adding a wire-level cap.
            layer.values["max_output_tokens"] = (
                max_output_tokens or DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
            )
            layer.field_sources["max_output_tokens"] = (
                source if max_output_tokens is not None else f"{source}:local-default"
            )
            layer.raw_metadata["max_output_tokens"] = layer.values["max_output_tokens"]
        if max_output_tokens is not None and "max_output_tokens" not in layer.values:
            layer.values["max_output_tokens"] = max_output_tokens
            layer.field_sources["max_output_tokens"] = source
            layer.raw_metadata["max_output_tokens"] = max_output_tokens
        if provider_id == "openai-codex" and "context_window_tokens" not in layer.values:
            layer.values["context_window_tokens"] = DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS
            layer.field_sources["context_window_tokens"] = source
            layer.values["max_output_tokens"] = DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
            layer.field_sources["max_output_tokens"] = f"{source}:local-default"
            layer.raw_metadata["context_window_tokens"] = layer.values["context_window_tokens"]
            layer.raw_metadata["max_output_tokens"] = layer.values["max_output_tokens"]
        layer.values["supports_vision"] = "image" in input_modalities
        layer.field_sources["supports_vision"] = source
        for field_name in _FLOAT_FIELDS:
            layer.values[field_name] = 0.0
            layer.field_sources[field_name] = f"{source}:included"
        return layer

    def _resolve_official_provider_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name=OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE)
        provider_key, route_base_url = self._provider_route_identity()
        normalized_provider = str(provider_key or "").strip().lower()
        provider_models = _OFFICIAL_PROVIDER_MODEL_METADATA.get(normalized_provider)
        if provider_models is None:
            return layer
        model_match = _lookup_model_override(
            mapping=provider_models,
            requested_model=requested_model,
            label=f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}.{normalized_provider}",
            warnings=layer.warnings,
        )
        if model_match is None:
            return layer

        model_key, payload = model_match
        payload = dict(payload)
        if normalized_provider == "moonshot":
            try:
                route_host = (urlsplit(route_base_url).hostname or "").rstrip(".").lower()
            except ValueError:
                route_host = ""
            if route_host != "api.moonshot.ai":
                # The verified prices are Moonshot's global USD rates. Capacity
                # and capabilities also apply to China, but those prices do not.
                for field_name in _FLOAT_FIELDS:
                    payload.pop(field_name, None)
        source = f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:{normalized_provider}"
        layer.model_name = model_key
        layer.raw_metadata = {
            **payload,
            "provider": normalized_provider,
            "catalog_source": OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE,
            "catalog_sources": list(
                _OFFICIAL_PROVIDER_MODEL_SOURCES.get(normalized_provider, {}).get(model_key, ())
            ),
        }
        self._apply_user_scope(layer=layer, source=source, payload=payload)
        return layer

    def _resolve_canonical_model_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name=CANONICAL_MODEL_CATALOG_SOURCE)
        provider_key, _route_base_url = self._provider_route_identity()
        normalized_provider = str(provider_key or "").strip().lower()

        canonical_id: str | None = None
        provider_aliases = _PROVIDER_CANONICAL_MODEL_IDS.get(normalized_provider, {})
        provider_alias_index = _build_model_alias_index(provider_aliases)
        for variant in model_name_variants(requested_model):
            route_id = provider_alias_index.get(variant.casefold())
            if route_id is not None:
                canonical_id = provider_aliases.get(route_id)
                break

        if canonical_id is None:
            canonical_alias_index = _build_model_alias_index(_CANONICAL_MODEL_METADATA)
            for variant in model_name_variants(requested_model):
                canonical_id = canonical_alias_index.get(variant.casefold())
                if canonical_id is not None:
                    break
        if canonical_id is None:
            return layer

        payload = _CANONICAL_MODEL_METADATA.get(canonical_id)
        if payload is None:
            return layer
        source = f"{CANONICAL_MODEL_CATALOG_SOURCE}:{canonical_id}"
        layer.raw_metadata = {
            **payload,
            "canonical_model": canonical_id,
            "canonical_catalog_sources": list(_CANONICAL_MODEL_SOURCES.get(canonical_id, ())),
        }
        self._apply_user_scope(layer=layer, source=source, payload=payload)
        return layer

    def _resolve_builtin_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name="built_in")
        model_match = _lookup_model_override(
            mapping=_BUILT_IN_MODEL_METADATA,
            requested_model=requested_model,
            label="built_in_model_catalog",
            warnings=layer.warnings,
        )
        if model_match is None:
            return layer

        model_key, payload = model_match
        layer.model_name = model_key
        layer.raw_metadata = dict(payload)
        self._apply_user_scope(layer=layer, source="built_in", payload=payload)
        return layer

    def _resolve_learned_layer(self, requested_model: str) -> _LayerData:
        _ = requested_model
        # Placeholder layer for deterministic precedence; learned cache is not implemented yet.
        return _LayerData(name="learned")

    def _resolve_field_value(self, field_name: str, layers: list[_LayerData]) -> tuple[Any, str]:
        for layer in layers:
            if field_name not in layer.values:
                continue
            value = layer.values[field_name]
            if value is None:
                continue
            return value, layer.field_sources.get(field_name, layer.name)
        return _FALLBACKS[field_name], "fallback"

    def get(self, model_name: str, *, include_provider_auth: bool = True) -> ModelMeta:
        requested = model_name.strip() or self._cfg.model.strip() or "unknown-model"
        warnings: list[str] = []
        if "model_capabilities" in self._cfg.extra_fields:
            warnings.append(_DEPRECATED_MODEL_CAPABILITIES_WARNING)

        env_layer = self._resolve_env_layer()
        user_layer = self._resolve_user_layer(requested)
        provider_auth_layer = (
            self._resolve_provider_auth_layer(requested)
            if include_provider_auth
            else _LayerData(name="provider_auth")
        )
        official_provider_layer = self._resolve_official_provider_layer(requested)
        bundled_catalog_layer = self._resolve_bundled_model_catalog_layer(requested)
        canonical_model_layer = self._resolve_canonical_model_layer(requested)
        route_provider, route_base_url = self._provider_route_identity()
        active_provider = (
            str(route_provider or known_provider_key_from_base_url(route_base_url) or "")
            .strip()
            .lower()
        )
        bundled_provider = (
            str(bundled_catalog_layer.raw_metadata.get("litellm_provider") or "").strip().lower()
        )
        catalog_route_provider = (
            str(bundled_catalog_layer.raw_metadata.get("catalog_provider_hint") or "")
            .strip()
            .lower()
            or bundled_provider
        )
        if catalog_route_provider and catalog_route_provider != active_provider:
            # Catalog prices belong to the route selected by the catalog lookup.
            # A model-name match may still supply portable capability data, but
            # it cannot establish prices for an unrelated compatible endpoint.
            for field_name in _FLOAT_FIELDS:
                bundled_catalog_layer.values.pop(field_name, None)
                bundled_catalog_layer.field_sources.pop(field_name, None)
            bundled_catalog_layer.raw_metadata = {}
        built_in_layer = self._resolve_builtin_layer(requested)
        learned_layer = self._resolve_learned_layer(requested)
        layers = [
            env_layer,
            user_layer,
            provider_auth_layer,
            official_provider_layer,
            canonical_model_layer,
            bundled_catalog_layer,
            built_in_layer,
            learned_layer,
        ]

        for layer in layers:
            warnings.extend(layer.warnings)

        field_sources: dict[str, str] = {}
        resolved_fields: dict[str, Any] = {}
        for field_name in _TRACKED_FIELDS:
            value, source = self._resolve_field_value(field_name, layers)
            resolved_fields[field_name] = value
            field_sources[field_name] = source

        context_window_tokens = (
            parse_positive_int(resolved_fields["context_window_tokens"])
            or _FALLBACKS["context_window_tokens"]
        )
        max_output_tokens = (
            parse_positive_int(resolved_fields["max_output_tokens"])
            or _FALLBACKS["max_output_tokens"]
        )
        supports_vision = (
            parse_bool(resolved_fields["supports_vision"])
            if resolved_fields["supports_vision"] is not None
            else _FALLBACKS["supports_vision"]
        )
        if supports_vision is None:
            supports_vision = _FALLBACKS["supports_vision"]
        supports_reasoning = (
            parse_bool(resolved_fields["supports_reasoning"])
            if resolved_fields["supports_reasoning"] is not None
            else None
        )
        input_cost_per_token = (
            parse_non_negative_float(resolved_fields["input_cost_per_token"])
            if resolved_fields["input_cost_per_token"] is not None
            else None
        )
        output_cost_per_token = (
            parse_non_negative_float(resolved_fields["output_cost_per_token"])
            if resolved_fields["output_cost_per_token"] is not None
            else None
        )
        cache_read_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_read_input_cost_per_token"])
            if resolved_fields["cache_read_input_cost_per_token"] is not None
            else None
        )
        cache_creation_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_creation_input_cost_per_token"])
            if resolved_fields["cache_creation_input_cost_per_token"] is not None
            else None
        )
        cache_creation_5m_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_creation_5m_input_cost_per_token"])
            if resolved_fields["cache_creation_5m_input_cost_per_token"] is not None
            else None
        )
        cache_creation_1h_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_creation_1h_input_cost_per_token"])
            if resolved_fields["cache_creation_1h_input_cost_per_token"] is not None
            else None
        )
        reasoning_output_cost_per_token = (
            parse_non_negative_float(resolved_fields["reasoning_output_cost_per_token"])
            if resolved_fields["reasoning_output_cost_per_token"] is not None
            else None
        )

        if max_output_tokens >= context_window_tokens:
            # Shared-window metadata (e.g. the Kimi Code ids publish max_tokens
            # up to the full context). Clamping to window-1 — the old behaviour —
            # left a 1-token input budget, which surfaced as "context: 0% left"
            # on a fresh 1M-context session. Reserve a conservative response
            # allowance instead so the input budget keeps most of the window.
            clamped = max(1, min(max_output_tokens, max(4096, context_window_tokens // 8)))
            warnings.append(
                "max_output_tokens >= context_window_tokens (shared window); "
                f"reserving {clamped} tokens for output."
            )
            max_output_tokens = clamped

        key_fallback = (
            field_sources.get("context_window_tokens") == "fallback"
            or field_sources.get("max_output_tokens") == "fallback"
        )
        if key_fallback:
            warnings.append(_FALLBACK_WARNING)

        all_sources = {field_sources.get(field_name, "fallback") for field_name in _TRACKED_FIELDS}
        overall_source = next(iter(all_sources)) if len(all_sources) == 1 else "mixed"

        resolved_model_name = next(
            (
                layer.model_name
                for layer in layers
                if layer.values and isinstance(layer.model_name, str) and layer.model_name.strip()
            ),
            requested,
        )

        final_warnings = tuple(_dedupe_warnings(warnings))
        self.last_error = bundled_catalog_layer.error if key_fallback else None
        self.last_warnings = list(final_warnings)
        self.last_source = overall_source

        raw_metadata = dict(bundled_catalog_layer.raw_metadata)
        raw_metadata.update(canonical_model_layer.raw_metadata)
        raw_metadata.update(official_provider_layer.raw_metadata)
        raw_metadata.update(provider_auth_layer.raw_metadata)
        return ModelMeta(
            model_name=resolved_model_name,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            input_cost_per_token=input_cost_per_token,
            output_cost_per_token=output_cost_per_token,
            cache_read_input_cost_per_token=cache_read_input_cost_per_token,
            cache_creation_input_cost_per_token=cache_creation_input_cost_per_token,
            cache_creation_5m_input_cost_per_token=cache_creation_5m_input_cost_per_token,
            cache_creation_1h_input_cost_per_token=cache_creation_1h_input_cost_per_token,
            reasoning_output_cost_per_token=reasoning_output_cost_per_token,
            raw_metadata=raw_metadata,
            source=overall_source,
            supports_vision=bool(supports_vision),
            supports_reasoning=supports_reasoning,
            field_sources=field_sources,
            warnings=final_warnings,
            provider_key=self._provider_hint(),
        )


def resolve_model_provider_key(
    *,
    cfg: AppConfig,
    model_name: str,
    base_url: str | None = None,
    profile_name: str | None = None,
) -> str | None:
    requested_model = str(model_name or "").strip()
    resolved_base_url = base_url or getattr(cfg, "base_url", None)
    try:
        from .profile_presets import find_preset_for_base_url

        preset = find_preset_for_base_url(str(resolved_base_url or ""))
    except Exception:  # noqa: BLE001 - provider inference remains best effort
        preset = None
    preset_provider = str(preset.provider_key or "").strip() if preset is not None else ""
    if preset_provider:
        return preset_provider

    url_provider = _provider_key_from_base_url(resolved_base_url)
    if url_provider == "openrouter":
        return url_provider

    if requested_model:
        meta = ModelRegistry(cfg=cfg).get(requested_model)
        provider = str(meta.raw_metadata.get("litellm_provider") or "").strip()
        if provider:
            return provider
        resolved_model = str(meta.model_name or "").strip()
        if "/" in resolved_model:
            return resolved_model.split("/", 1)[0].strip() or None

    if url_provider:
        return url_provider

    profile_provider = str(profile_name or "").strip()
    if profile_provider and profile_provider.lower() != "default":
        return profile_provider

    if "/" in requested_model:
        return requested_model.split("/", 1)[0].strip() or None
    return requested_model or None


def _provider_key_from_base_url(base_url: str | None) -> str | None:
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        hostname = (urlsplit(raw).hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if not hostname:
        return None
    known_provider = known_provider_key_from_base_url(raw)
    if known_provider:
        return known_provider
    parts = [
        part
        for part in hostname.split(".")
        if part and part not in {"api", "ai", "www", "com", "v1", "v1beta"}
    ]
    return parts[-1] if parts else hostname
