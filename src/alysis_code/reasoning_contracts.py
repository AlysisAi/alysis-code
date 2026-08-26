"""Static per-model reasoning contracts — the wire truth table.

Source: ``MODEL_CAPABILITY_CONTRACTS_2026-07-19.md`` (19 provider blocks, each
adversarially verified against official docs). This module is step 1 of that
report's implementation order: the data nothing at runtime can discover —
whether a model's reasoning is ``always-on`` / ``optional`` / ``none``, the
exact wire spelling that controls it, the allowed value strings, and what
"off" actually does (omit the param, send an explicit disable, impossible, or
— worst — silently swap the model).

Emission rule this table exists to enforce (report hazard ranks 5, 8, 9, 12,
16, 18): **allowlist-based emission** — a client may send a reasoning
parameter only when the contract says this model accepts that exact spelling
and value. ``UNKNOWN`` means send nothing.

The mechanisms here are provider-agnostic; only the data is per-provider.
Clients keep their own payload shaping (e.g. ``anthropic_messages`` builds the
``thinking`` config) and consult this table for the *decision*.
"""

from __future__ import annotations

from dataclasses import dataclass

# Reasoning modes.
ALWAYS_ON = "always-on"
OPTIONAL = "optional"
NONE = "none"
UNKNOWN = "unknown"

# Wire spellings (how the knob is expressed on this surface).
WIRE_REASONING_EFFORT = "reasoning_effort"  # flat string param (openai-chat style)
WIRE_THINKING_TYPE = "thinking_type"  # thinking: {"type": ...}
WIRE_THINKING_ADAPTIVE = "thinking_adaptive"  # thinking:{"type":"adaptive"} + output_config effort
WIRE_BUDGET_TOKENS = "budget_tokens"  # thinking:{"type":"enabled","budget_tokens":N}
WIRE_THINKING_LEVEL = "thinking_level"  # generationConfig.thinkingConfig.thinkingLevel
WIRE_ENABLE_THINKING = "enable_thinking"  # boolean (+ optional thinking_budget)
WIRE_CHAT_TEMPLATE_ENABLE_THINKING = (
    "chat_template_enable_thinking"  # chat_template_kwargs: {enable_thinking: bool}
)
WIRE_REASONING_OBJECT = "reasoning_object"  # reasoning: {effort|max_tokens|enabled|exclude}
WIRE_REASONING_ENABLED = "reasoning_enabled"  # reasoning: {"enabled": bool}
WIRE_NONE = "none"  # no reasoning knob exists on this surface

# Off-path semantics.
OFF_OMIT = "omit-param"  # leaving the param out means no thinking
OFF_EXPLICIT = "explicit-disable"  # a documented disable value exists
OFF_IMPOSSIBLE = "impossible"  # reasoning cannot be turned off (disable = error)
OFF_SWAPS_MODEL = "swaps-model"  # "off" silently substitutes a different model
OFF_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReasoningContract:
    """What one model accepts on one provider surface."""

    mode: str = UNKNOWN
    wire: str = WIRE_NONE
    values: tuple[str, ...] = ()  # exact allowed effort/level strings ("" knob → empty)
    default: str = ""  # server default value; "" when unknown / not applicable
    off: str = OFF_UNKNOWN
    replay_reasoning_content: bool = False
    accepts_tool_choice_while_reasoning: bool = True
    emits_flat_reasoning_effort: bool = False  # verified on Chat Completions
    notes: str = ""

    def allows_value(self, value: str | None) -> bool:
        normalized = str(value or "").strip().casefold()
        if not normalized:
            return False
        return normalized in self.values

    @property
    def toggleable(self) -> bool:
        return self.mode == OPTIONAL and self.off in {OFF_OMIT, OFF_EXPLICIT}


UNKNOWN_CONTRACT = ReasoningContract()


def reasoning_labels_allowed_by_contract(
    contract: ReasoningContract,
    labels: list[str] | tuple[str, ...],
    *,
    current: str,
) -> list[str]:
    """Filter a reasoning picker to the active model's documented contract."""

    if contract is UNKNOWN_CONTRACT:
        return list(labels)
    effort_wires = {
        WIRE_REASONING_EFFORT,
        WIRE_THINKING_LEVEL,
        WIRE_THINKING_ADAPTIVE,
    }
    filter_by_values = bool(contract.values) and contract.wire in effort_wires
    toggle_only = contract.wire == WIRE_CHAT_TEMPLATE_ENABLE_THINKING
    allowed: list[str] = []
    for label in labels:
        normalized = label.casefold()
        if label == current:
            allowed.append(label)
            continue
        if normalized == "off":
            if contract.mode == ALWAYS_ON and contract.off != OFF_SWAPS_MODEL:
                continue
            allowed.append(label)
            continue
        if normalized == "auto":
            allowed.append(label)
            continue
        if toggle_only:
            continue
        if filter_by_values and normalized not in contract.values:
            continue
        allowed.append(label)
    return allowed


# Per provider_key: ordered (prefix, contract) rules; first match wins. Exact
# ids sort naturally before shorter prefixes where it matters.
_C = ReasoningContract
_CONTRACTS: dict[str, tuple[tuple[str, ReasoningContract], ...]] = {
    "openai": (
        # tools + reasoning_effort != "none" on /v1/chat/completions is a 400
        # on the 5.6/5.4 families (report hazard #1) — the *value* allowlist is
        # identical on both endpoints; routing is the client's decision.
        (
            "gpt-5.6-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_EXPLICIT,
                notes="chat surface 400s on tools + effort!=none; 'minimal' is dead on 5.x",
            ),
        ),
        (
            "gpt-5.4-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high", "xhigh", "max"),
                off=OFF_EXPLICIT,
                notes="same chat-surface tools+effort 400 as 5.6 when effort raised",
            ),
        ),
        (
            "gpt-5.3-codex",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh"),
                off=OFF_IMPOSSIBLE,
                notes="effort 'none' is a 400 unsupported_value",
            ),
        ),
    ),
    "anthropic": (
        (
            "claude-fable-5",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_IMPOSSIBLE,
                notes="thinking:{'type':'disabled'} is rejected; always adaptive",
            ),
        ),
        (
            "claude-haiku-4-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_BUDGET_TOKENS,
                off=OFF_EXPLICIT,
                notes="extended thinking only (budget_tokens >= 1024); no effort param",
            ),
        ),
        (
            "claude-sonnet-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_OMIT,
                notes="extended shape = 400; non-default temperature/top_p/top_k = 400",
            ),
        ),
        (
            "claude-opus-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="thinks by DEFAULT — omitting the param is adaptive, not off "
                "(unlike 4.8/4.7); disable is accepted only at effort <= high, so "
                "'disabled' + xhigh/max = 400; extended shape and non-default "
                "sampling params = 400",
            ),
        ),
        (
            "claude-opus-4-8",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_OMIT,
                notes="extended shape = 400; non-default sampling params = 400",
            ),
        ),
        (
            "claude-opus-4-7",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_OMIT,
                notes="extended shape = 400; non-default sampling params = 400",
            ),
        ),
    ),
    "gemini": (
        (
            "gemini-3.7-flash",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_LEVEL,
                values=("low", "medium", "high"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                notes="'minimal' is explicitly unsupported on Gemini 3.7 Flash",
            ),
        ),
        (
            "gemini-3.1-pro",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_LEVEL,
                values=("low", "medium", "high"),
                off=OFF_IMPOSSIBLE,
                notes="'minimal' documented Not supported on 3.1 Pro",
            ),
        ),
        (
            "gemini-3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_LEVEL,
                values=("minimal", "low", "medium", "high"),
                off=OFF_IMPOSSIBLE,
                notes="no 3.x model can disable thinking; economy calls pin 'minimal'",
            ),
        ),
    ),
    "deepseek": (
        (
            "deepseek-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                replay_reasoning_content=True,
                accepts_tool_choice_while_reasoning=False,
                notes="low|high|max are the exact effort values; thinking:{type} is the "
                "separate on/off toggle; "
                "reasoning_content is required across every tools-enabled assistant turn; "
                "thinking mode rejects tool_choice; compatibility aliases are not exposed",
            ),
        ),
    ),
    # The Alysis Code hosted proxy forwards request bodies verbatim to DeepSeek,
    # so its models follow the deepseek contract exactly (same wire, same
    # levels, same off semantics).
    "alysis": (
        (
            "deepseek-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                replay_reasoning_content=True,
                accepts_tool_choice_while_reasoning=False,
                notes="hosted proxy passes reasoning fields through to DeepSeek unchanged; "
                "mirrors the deepseek provider contract",
            ),
        ),
    ),
    "nvidia": (
        (
            "deepseek-ai/deepseek-v4-pro",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA-hosted DeepSeek V4 Pro accepts none|high|max on the flat "
                "reasoning_effort field",
            ),
        ),
        (
            "deepseek-ai/deepseek-v4-flash",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA-hosted DeepSeek V4 Flash accepts none|high|max on the flat "
                "reasoning_effort field",
            ),
        ),
        (
            "nvidia/nemotron-3-super-120b-a12b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA's hosted endpoint accepts none|low|high and returns private "
                "reasoning on the structured reasoning_content channel",
            ),
        ),
        (
            "nvidia/nemotron-3-ultra-550b-a55b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("medium", "high"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA's hosted endpoint accepts none|medium|high",
            ),
        ),
        (
            "nvidia/nemotron-3-nano-30b-a3b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_CHAT_TEMPLATE_ENABLE_THINKING,
                off=OFF_EXPLICIT,
                notes="the hosted endpoint documents the chat-template toggle, not a flat "
                "reasoning_effort enum",
            ),
        ),
    ),
    "qwen": (
        (
            "qwen3.8-max",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "xhigh"),
                default="xhigh",
                off=OFF_EXPLICIT,
                replay_reasoning_content=True,
                notes="effort is flat; enable_thinking is the separate on/off toggle",
            ),
        ),
        (
            "qwen3-coder",
            _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT, notes="coder line has no thinking"),
        ),
        (
            "qwen",
            _C(
                mode=OPTIONAL,
                wire=WIRE_ENABLE_THINKING,
                off=OFF_EXPLICIT,
                notes="3.7 line thinks by default — omitting the flag bills thinking tokens",
            ),
        ),
    ),
    "zhipu": (
        (
            "glm-5.2",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_TYPE,
                values=("high",),
                off=OFF_EXPLICIT,
                notes="reasoning_effort low/medium coerce to high; OpenRouter-style "
                "reasoning:{} object is silently ignored",
            ),
        ),
        (
            "glm-4.7-flash",
            _C(mode=OPTIONAL, wire=WIRE_THINKING_TYPE, off=OFF_EXPLICIT),
        ),
        (
            "glm-4.7",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_TYPE,
                off=OFF_IMPOSSIBLE,
                notes="thinks compulsorily despite a documented toggle",
            ),
        ),
        (
            "glm-",
            _C(mode=OPTIONAL, wire=WIRE_THINKING_TYPE, off=OFF_EXPLICIT),
        ),
    ),
    "zai_coding_plan": (
        # Sources: https://docs.z.ai/guides/llm/glm-5.3
        # https://docs.z.ai/guides/llm/glm-5-turbo
        # https://docs.z.ai/guides/llm/glm-4.7
        (
            "glm-5.3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes=(
                    "Coding Plan surface: disabling thinking is coerced to low rather than "
                    "turning reasoning off; aliases are normalized server-side"
                ),
            ),
        ),
        (
            "glm-5-turbo",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="Coding Plan reasoning floor follows the shared effort normalizer",
            ),
        ),
        (
            "glm-4.7",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="Coding Plan reasoning floor follows the shared effort normalizer",
            ),
        ),
    ),
    "moonshot": (
        # Platform surface (api.moonshot.ai / .cn). The kimi-code membership
        # surface has different contracts — provider_key "kimi-code" below.
        # Source: https://www.kimi.com/help/kimi-api/api-model-selection
        (
            "kimi-k2.7-code",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_TYPE,
                values=("enabled",),
                off=OFF_IMPOSSIBLE,
                notes="thinking:{'type':'disabled'} = 400 'invalid thinking'; "
                "temperature!=1.0 or top_p!=0.95 = 400",
            ),
        ),
        (
            "kimi-k3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="uses reasoning_effort, not thinking; do not send the K2.x param",
            ),
        ),
        (
            "kimi-k2.6",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_TYPE,
                off=OFF_EXPLICIT,
                notes="the only toggleable model on this surface; replay reasoning_content "
                "in tool loops",
            ),
        ),
    ),
    "kimi-code": (
        # Source: https://www.kimi.com/code/docs/en/kimi-code/models.html
        (
            "k3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="high",
                off=OFF_SWAPS_MODEL,
                emits_flat_reasoning_effort=True,
                notes="disabling thinking silently routes the request to K2.6 — surface "
                "the substitution, never treat it as a speed knob",
            ),
        ),
        (
            "kimi-for-coding",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_TYPE,
                values=("enabled",),
                off=OFF_SWAPS_MODEL,
                notes="thinking defaults on; 'off' is a silent K2.6 substitution",
            ),
        ),
    ),
    "minimax": (
        (
            "MiniMax-M3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_TYPE,
                values=("enabled", "adaptive", "disabled"),
                off=OFF_EXPLICIT,
                notes="api-reference enum lists only disabled|adaptive (conflict recorded); "
                "anthropic surface defaults thinking DISABLED",
            ),
        ),
        (
            "MiniMax-M2",
            _C(
                mode=NONE,
                wire=WIRE_NONE,
                off=OFF_OMIT,
                notes="M2.x exposes no reasoning control — send nothing",
            ),
        ),
    ),
    "bytedance": (
        (
            "doubao-",
            _C(
                mode=UNKNOWN,
                wire=WIRE_THINKING_TYPE,
                off=OFF_UNKNOWN,
                notes="probe-gated; reasoning_effort 'high' live-observed as 400 "
                "InvalidParameter on the coding surface — send nothing until probed",
            ),
        ),
    ),
    "groq": (
        # Source: https://console.groq.com/docs/reasoning
        (
            "groq/compound",
            _C(
                mode=NONE,
                wire=WIRE_NONE,
                off=OFF_OMIT,
                notes="also rejects client-provided tools entirely",
            ),
        ),
        (
            "qwen/qwen3.6-27b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "default"),
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
            ),
        ),
        (
            "openai/gpt-oss-",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="reasoning_format 'raw' + tools/JSON = 400; "
                "include_reasoning + reasoning_format together = 400",
            ),
        ),
    ),
    "cerebras": (
        # Source: https://inference-docs.cerebras.ai/api-reference/chat-completions
        (
            "gpt-oss-120b",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="no 'none'; disable_reasoning dead since 2026-07-21; "
                "tools + response_format together = rejected",
            ),
        ),
        (
            "zai-glm-4.7",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none",),
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
                notes="only 'none' is documented as a control value; low/medium/high are not "
                "documented on this Cerebras model",
            ),
        ),
        (
            "gemma-4-31b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high"),
                default="none",
                off=OFF_EXPLICIT,
            ),
        ),
    ),
    "mistral": (
        (
            "mistral-medium-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high"),
                off=OFF_EXPLICIT,
                notes="only 'high'/'none' have documented semantics; content becomes "
                "typed chunks (thinking/text) while reasoning — replay ThinkChunks",
            ),
        ),
        (
            "mistral-small-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high"),
                off=OFF_EXPLICIT,
            ),
        ),
        ("mistral-large-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
        ("codestral-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
        ("ministral-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
    ),
    "xai": (
        # The current xAI reasoning guide documents the flat field for its SDK,
        # while the legacy Chat Completions reference does not. Keep emission
        # off until that transport surface is explicitly documented.
        # Sources: https://docs.x.ai/developers/model-capabilities/text/reasoning
        # https://docs.x.ai/developers/model-capabilities/legacy/chat-completions
        (
            "grok-4.6",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh"),
                default="high",
                off=OFF_IMPOSSIBLE,
                notes="effort + stop/presence_penalty/frequency_penalty = documented error",
            ),
        ),
        (
            "grok-4.5",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high"),
                default="high",
                off=OFF_IMPOSSIBLE,
                notes="effort + stop/presence_penalty/frequency_penalty = documented error",
            ),
        ),
        (
            "grok-build-0.1",
            _C(mode=ALWAYS_ON, wire=WIRE_REASONING_EFFORT, off=OFF_IMPOSSIBLE),
        ),
        (
            "grok-4.20-0309-reasoning",
            _C(mode=ALWAYS_ON, wire=WIRE_NONE, off=OFF_IMPOSSIBLE, notes="mode-by-slug"),
        ),
        (
            "grok-4.20-0309-non-reasoning",
            _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT, notes="mode-by-slug"),
        ),
        (
            "grok-4.3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                default="low",
                off=OFF_EXPLICIT,
                notes="has an off switch; full value set unpublished — emit only "
                "values confirmed by probe",
            ),
        ),
    ),
    "cohere": (
        # Source: https://docs.cohere.com/v2/docs/compatibility-api
        (
            "command-a-reasoning-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high"),
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
                notes="medium/low documented not supported on the compat surface; native "
                "thinking param likely does not pass through /compatibility/v1",
            ),
        ),
        ("command-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
    ),
    "openrouter": (
        (
            "",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_OBJECT,
                off=OFF_UNKNOWN,
                notes="drive from live /api/v1/models: per-model supported_efforts + "
                "default_effort; effort and max_tokens are mutually exclusive",
            ),
        ),
    ),
    "perplexity": (
        (
            "sonar",
            _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT, notes="sonar rejects tools anyway"),
        ),
    ),
    "together": (
        (
            "deepseek-ai/DeepSeek-V4-Pro",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="reasoning_effort controls depth; reasoning:{enabled:false} is the "
                "separate off wire shape; reasoning must be replayed after tool calls",
            ),
        ),
        (
            "moonshotai/Kimi-K2.7-Code",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_IMPOSSIBLE,
                notes="thinking cannot be disabled on Together",
            ),
        ),
        (
            "MiniMaxAI/MiniMax-M3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_EXPLICIT,
                notes="toggleable per Together's model page; exact wire spelling "
                "unconfirmed — probe before emitting",
            ),
        ),
        (
            "deepseek-ai/DeepSeek-V4-Flash",
            UNKNOWN_CONTRACT,
        ),
        (
            "deepseek-ai/",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_EXPLICIT,
                notes="effort low/medium coerce to high",
            ),
        ),
        (
            "openai/gpt-oss-",
            _C(mode=OPTIONAL, wire=WIRE_REASONING_ENABLED, off=OFF_EXPLICIT),
        ),
        (
            "zai-org/",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_UNKNOWN,
                notes="not classified by the capability report — probe before emitting",
            ),
        ),
    ),
    "fireworks": (
        (
            "accounts/fireworks/models/deepseek-v4-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
                notes="low/medium normalize to high and xhigh normalizes to max; expose only "
                "the three distinct wire behaviors",
            ),
        ),
        # The endpoint accepts reasoning_effort, but the current reference does
        # not define values for MiniMax M3. Leave this model probe-gated.
        # Source: https://docs.fireworks.ai/api-reference/post-chatcompletions
        (
            "accounts/fireworks/models/minimax-m3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                off=OFF_UNKNOWN,
                notes="doc-classified adaptive; thinking + reasoning_effort together = "
                "documented validation error",
            ),
        ),
        (
            "accounts/fireworks/models/",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_EFFORT,
                off=OFF_UNKNOWN,
                notes="per-model acceptance probe-gated (5 of 6 unknown)",
            ),
        ),
    ),
}


def reasoning_contract_for(
    provider_key: str | None, model: str | None, *, preset_key: str | None = None
) -> ReasoningContract:
    """Resolve the reasoning contract for ``model`` on ``provider_key``.

    ``preset_key`` takes precedence when it names a distinct surface — e.g. the
    ``kimi-code`` preset carries ``provider_key="moonshot"``, but the membership
    endpoint's contracts differ from the platform's (same vendor, different
    rules; "off" even means different things). First matching prefix rule wins;
    anything unmatched returns :data:`UNKNOWN_CONTRACT` — and per the allowlist
    emission rule, unknown means *send no reasoning parameter at all*.
    """
    name = str(model or "").strip()
    if not name:
        return UNKNOWN_CONTRACT
    folded = name.casefold()
    for key in (preset_key, provider_key):
        rules = _CONTRACTS.get(str(key or "").strip())
        if not rules:
            continue
        for prefix, contract in rules:
            if folded.startswith(prefix.casefold()):
                return contract
    return UNKNOWN_CONTRACT


def reasoning_off_is_safe(provider_key: str | None, model: str | None) -> bool:
    """Whether a reasoning-off toggle can be honoured without error or deceit."""
    return reasoning_contract_for(provider_key, model).toggleable


def reasoning_off_hazard(provider_key: str | None, model: str | None) -> str:
    """Human-readable reason a reasoning-off toggle must not be emitted.

    Empty string when off is safe. Drives the TUI warning (and the
    ``doctor --live`` assertion mode, per implementation-order step 7).
    """
    contract = reasoning_contract_for(provider_key, model)
    if contract.toggleable:
        return ""
    if contract.off == OFF_SWAPS_MODEL:
        return f"disabling reasoning on {model} silently substitutes a different model"
    if contract.off == OFF_IMPOSSIBLE:
        return f"{model} cannot disable reasoning; lowest effort is the floor"
    if contract.mode == NONE:
        return f"{model} has no reasoning control; nothing to disable"
    return f"reasoning contract for {model} is unknown; emitting a disable flag is unsafe"


__all__ = [
    "ALWAYS_ON",
    "NONE",
    "OPTIONAL",
    "UNKNOWN",
    "UNKNOWN_CONTRACT",
    "OFF_EXPLICIT",
    "OFF_IMPOSSIBLE",
    "OFF_OMIT",
    "OFF_SWAPS_MODEL",
    "OFF_UNKNOWN",
    "ReasoningContract",
    "WIRE_BUDGET_TOKENS",
    "WIRE_ENABLE_THINKING",
    "WIRE_CHAT_TEMPLATE_ENABLE_THINKING",
    "WIRE_NONE",
    "WIRE_REASONING_EFFORT",
    "WIRE_REASONING_ENABLED",
    "WIRE_REASONING_OBJECT",
    "WIRE_THINKING_ADAPTIVE",
    "WIRE_THINKING_LEVEL",
    "WIRE_THINKING_TYPE",
    "reasoning_contract_for",
    "reasoning_labels_allowed_by_contract",
    "reasoning_off_hazard",
    "reasoning_off_is_safe",
]
