from __future__ import annotations

from typing import Any

import pytest

from alysis_code.agent.llm_calls import (
    _main_agent_chat,
    _safe_forced_tool_choice_for_recovery,
)
from alysis_code.llm.openai_compat import LLMResponse, OpenAICompatClient


def _tool_schema(name: str = "diagnostic_echo") -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
        }
    ]


class _RecordingClient:
    supports_tool_calling = False
    supports_forced_tool_choice = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        return LLMResponse(content="ok", tool_calls=[], raw={})


def test_main_agent_chat_omits_tools_after_dynamic_tool_calling_rejection() -> None:
    client = _RecordingClient()

    response = _main_agent_chat(
        client=client,
        messages=[{"role": "user", "content": "hi"}],
        tools=_tool_schema(),
        stream=False,
        on_text_delta=None,
        tool_choice={"type": "function", "function": {"name": "diagnostic_echo"}},
    )

    assert response.content == "ok"
    assert client.calls[0]["tools"] is None
    assert "tool_choice" not in client.calls[0]


def test_main_agent_chat_preserves_legacy_client_without_cancellation_keyword() -> None:
    class _LegacyClient:
        supports_tool_calling = True

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            *,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
            stream: bool,
            on_text_delta: Any,
            on_reasoning_delta: Any,
            temperature: float | None,
        ) -> LLMResponse:
            _ = messages, tools, stream, on_text_delta, on_reasoning_delta, temperature
            self.calls += 1
            return LLMResponse(content="legacy", tool_calls=[], raw={})

    client = _LegacyClient()

    response = _main_agent_chat(
        client=client,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        stream=False,
        on_text_delta=None,
        cancellation_token=object(),
    )

    assert response.content == "legacy"
    assert client.calls == 1


def test_main_agent_chat_does_not_replay_internal_type_error() -> None:
    class _BrokenClient:
        supports_tool_calling = True

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, **_kwargs: Any) -> LLMResponse:
            self.calls += 1
            raise TypeError("internal provider adapter bug")

    client = _BrokenClient()

    with pytest.raises(TypeError, match="internal provider adapter bug"):
        _main_agent_chat(
            client=client,
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            stream=False,
            on_text_delta=None,
            cancellation_token=object(),
        )

    assert client.calls == 1


def test_main_agent_chat_does_not_infer_compatibility_from_type_error_text() -> None:
    class _BrokenClient:
        supports_tool_calling = True

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, **_kwargs: Any) -> LLMResponse:
            self.calls += 1
            raise TypeError("adapter() got an unexpected keyword argument 'temperature'")

    client = _BrokenClient()

    with pytest.raises(TypeError, match="unexpected keyword argument 'temperature'"):
        _main_agent_chat(
            client=client,
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            stream=False,
            on_text_delta=None,
            cancellation_token=object(),
        )

    assert client.calls == 1


def test_forced_tool_choice_requires_tool_calling_and_no_active_reasoning() -> None:
    client = type(
        "Client",
        (),
        {
            "supports_tool_calling": True,
            "supports_forced_tool_choice": True,
            "enable_thinking": True,
        },
    )()

    assert (
        _safe_forced_tool_choice_for_recovery(
            client=client,
            tools=_tool_schema(),
            preferred_tool_names=("diagnostic_echo",),
        )
        is None
    )

    client.enable_thinking = False
    assert _safe_forced_tool_choice_for_recovery(
        client=client,
        tools=_tool_schema(),
        preferred_tool_names=("diagnostic_echo",),
    ) == {"type": "function", "function": {"name": "diagnostic_echo"}}


def test_deepseek_default_reasoning_suppresses_agent_recovery_tool_choice() -> None:
    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test",
        model="deepseek-v4-pro",
    )

    assert client.reasoning_active is True
    assert (
        _safe_forced_tool_choice_for_recovery(
            client=client,
            tools=_tool_schema(),
            preferred_tool_names=("diagnostic_echo",),
        )
        is None
    )

    client.enable_thinking = False
    assert client.reasoning_active is False
    assert _safe_forced_tool_choice_for_recovery(
        client=client,
        tools=_tool_schema(),
        preferred_tool_names=("diagnostic_echo",),
    ) == {"type": "function", "function": {"name": "diagnostic_echo"}}
