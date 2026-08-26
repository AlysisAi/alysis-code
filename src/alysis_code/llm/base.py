from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from .types import InputTokenCount, LLMResponse, UsageContract


@runtime_checkable
class ChatClient(Protocol):
    """Protocol-neutral chat client surface used by the agent runtime."""

    base_url: str
    model: str
    supports_tool_calling: bool
    supports_forced_tool_choice: bool
    usage_contract: UsageContract

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class InputTokenCountingClient(Protocol):
    usage_contract: UsageContract

    def count_input_tokens(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> InputTokenCount | None: ...


def effective_tools_for_client(
    client: Any,
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Return only tools the client's current transport state can send."""

    return None if getattr(client, "supports_tool_calling", True) is False else tools


def count_input_tokens_if_supported(
    *,
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
) -> InputTokenCount | None:
    contract = getattr(client, "usage_contract", None)
    if not isinstance(contract, UsageContract) or not contract.supports_input_token_count:
        return None
    counter = getattr(client, "count_input_tokens", None)
    if not callable(counter):
        return None
    result = counter(messages=messages, tools=tools, tool_choice=tool_choice)
    return result if isinstance(result, InputTokenCount) else None
