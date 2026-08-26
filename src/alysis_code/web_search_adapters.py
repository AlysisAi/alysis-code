from __future__ import annotations

AUTO_WEB_SEARCH_ADAPTER = "auto"
OPENAI_RESPONSES_ADAPTER = "openai_responses"
XAI_RESPONSES_ADAPTER = "xai_responses"
ANTHROPIC_MESSAGES_ADAPTER = "anthropic_messages"
GEMINI_GROUNDING_ADAPTER = "gemini_grounding"
OPENROUTER_WEB_ADAPTER = "openrouter_web"
DASHSCOPE_CHAT_ADAPTER = "dashscope_chat"
MOONSHOT_KIMI_ADAPTER = "moonshot_kimi"
ZHIPU_WEB_SEARCH_ADAPTER = "zhipu_web_search"
VOLCENGINE_WEB_SEARCH_ADAPTER = "volcengine_web_search"
PERPLEXITY_SONAR_ADAPTER = "perplexity_sonar"
GROQ_COMPOUND_ADAPTER = "groq_compound"
MISTRAL_CONVERSATIONS_ADAPTER = "mistral_conversations"
MINIMAX_CODING_PLAN_ADAPTER = "minimax_coding_plan"
COHERE_WEB_SEARCH_ADAPTER = "cohere_web_search"
TAVILY_ADAPTER = "tavily"
DDGS_ADAPTER = "ddgs"

NATIVE_WEB_SEARCH_ADAPTERS: frozenset[str] = frozenset(
    {
        OPENAI_RESPONSES_ADAPTER,
        XAI_RESPONSES_ADAPTER,
        ANTHROPIC_MESSAGES_ADAPTER,
        GEMINI_GROUNDING_ADAPTER,
        OPENROUTER_WEB_ADAPTER,
        DASHSCOPE_CHAT_ADAPTER,
        MOONSHOT_KIMI_ADAPTER,
        ZHIPU_WEB_SEARCH_ADAPTER,
        VOLCENGINE_WEB_SEARCH_ADAPTER,
        PERPLEXITY_SONAR_ADAPTER,
        GROQ_COMPOUND_ADAPTER,
        MISTRAL_CONVERSATIONS_ADAPTER,
        MINIMAX_CODING_PLAN_ADAPTER,
        COHERE_WEB_SEARCH_ADAPTER,
    }
)
EXTERNAL_WEB_SEARCH_ADAPTERS: frozenset[str] = frozenset({TAVILY_ADAPTER, DDGS_ADAPTER})

VALID_WEB_SEARCH_ADAPTERS: frozenset[str] = frozenset(
    {
        AUTO_WEB_SEARCH_ADAPTER,
        *NATIVE_WEB_SEARCH_ADAPTERS,
        *EXTERNAL_WEB_SEARCH_ADAPTERS,
    }
)
WEB_SEARCH_ADAPTER_CHOICES: tuple[str, ...] = tuple(sorted(VALID_WEB_SEARCH_ADAPTERS))


def web_search_adapter_is_native(adapter: str) -> bool:
    return str(adapter or "").strip().lower() in NATIVE_WEB_SEARCH_ADAPTERS


def web_search_adapter_is_external(adapter: str) -> bool:
    return str(adapter or "").strip().lower() in EXTERNAL_WEB_SEARCH_ADAPTERS


def normalize_web_search_adapter(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return AUTO_WEB_SEARCH_ADAPTER
    if value in VALID_WEB_SEARCH_ADAPTERS:
        return value
    allowed = ", ".join(WEB_SEARCH_ADAPTER_CHOICES)
    raise ValueError(f"web_search_adapter must be one of: {allowed}")
