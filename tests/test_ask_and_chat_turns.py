"""`/ask` one-turn read-only override and `/chat` minimal no-tools turns.

PR 4 of the router-removal migration: explicit, deterministic replacements for
the router's per-turn advisory-posture inference (`/ask`) and its small-talk
short-circuit (`/chat`). Both are user-selected, never inferred.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from alysis_code import cli as cli_mod
from alysis_code.agent.turn_path import CHAT_ONLY_SYSTEM_PROMPT
from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.chat import loop as chat_loop_mod
from alysis_code.cli_impl.chat.state import (
    _ChatExecutionRequest,
    _ChatPlanModeState,
    _ForgeChatState,
)
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.session_store import read_session_events


def _dispatch(input_text: str, *, session: Any, tmp_path: Path) -> Any:
    return chat_loop_mod._handle_chat_command_impl(
        cli_mod,
        input_text=input_text,
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(),
        forge_state=_ForgeChatState(),
        plan_mode_state=_ChatPlanModeState(),
    )


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


# ---------------------------------------------------------------------------
# /ask producer
# ---------------------------------------------------------------------------


def test_ask_returns_one_turn_readonly_request(tmp_path: Path) -> None:
    session = SimpleNamespace(mode="review")
    result = _dispatch("/ask what does the parser module do?", session=session, tmp_path=tmp_path)

    assert isinstance(result, _ChatExecutionRequest)
    assert result.instruction == "what does the parser module do?"
    assert result.mode_override == "readonly"
    assert result.restore_mode_after == "review"
    assert result.chat_only is False


def test_ask_in_readonly_mode_needs_no_override(tmp_path: Path) -> None:
    session = SimpleNamespace(mode="readonly")
    result = _dispatch("/ask anything risky here?", session=session, tmp_path=tmp_path)

    assert isinstance(result, _ChatExecutionRequest)
    assert result.instruction == "anything risky here?"
    assert result.mode_override is None
    assert result.restore_mode_after is None


def test_ask_without_text_prints_usage(tmp_path: Path) -> None:
    session = SimpleNamespace(mode="review")
    assert _dispatch("/ask", session=session, tmp_path=tmp_path) == "handled"


# ---------------------------------------------------------------------------
# /chat producer
# ---------------------------------------------------------------------------


def test_chat_command_is_retired(tmp_path: Path) -> None:
    # /chat retired in favor of the Ask persona (/mode ask); the notice is a
    # handled command, never an execution request. The chat_only run_turn
    # plumbing below stays accepted-and-ignored for one release.
    session = SimpleNamespace(mode="review")
    assert _dispatch("/chat hello there", session=session, tmp_path=tmp_path) == "handled"
    assert _dispatch("/chat", session=session, tmp_path=tmp_path) == "handled"


# ---------------------------------------------------------------------------
# chat_only turns in run_turn
# ---------------------------------------------------------------------------


class _CapturingChatClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        _ = stream, on_text_delta, temperature
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return LLMResponse(content=self.reply, tool_calls=[], raw={})


def _session(tmp_path: Path) -> Any:
    cfg = AppConfig(model="test-model")
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )


def test_chat_only_turn_uses_minimal_prompt_and_no_tools(tmp_path: Path, monkeypatch) -> None:
    session = _session(tmp_path)
    client = _CapturingChatClient("Hi! How can I help?")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("hello", chat_only=True)
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["tools"] is None
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][0]["content"] == CHAT_ONLY_SYSTEM_PROMPT
    assert call["messages"][-1] == {"role": "user", "content": "hello"}
    finals = _event_payloads(log_path, "final")
    assert len(finals) == 1
    assert finals[0]["content"] == "Hi! How can I help?"
    assert _event_payloads(log_path, "route_decision") == []
    operations = {
        str(payload.get("operation") or "") for payload in _event_payloads(log_path, "llm_usage")
    }
    assert "routing_llm" not in operations


def test_chat_only_turns_keep_conversation_continuity(tmp_path: Path, monkeypatch) -> None:
    session = _session(tmp_path)
    client = _CapturingChatClient("Nice to meet you, Alex.")
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("hi, I'm Alex", chat_only=True) == 0
        assert session.run_turn("what's my name?", chat_only=True) == 0
    finally:
        session.close()

    assert len(client.calls) == 2
    second_request_text = "\n".join(
        str(message.get("content") or "") for message in client.calls[1]["messages"]
    )
    assert "hi, I'm Alex" in second_request_text
    assert "Nice to meet you, Alex." in second_request_text


def test_one_turn_mode_override_applies_and_restores(tmp_path: Path) -> None:
    # The exact calls the chat loop makes when consuming an /ask request:
    # readonly applied before the turn, previous mode restored afterwards.
    session = _session(tmp_path)
    try:
        assert session.mode == "review"
        chat_loop_mod._apply_chat_effective_mode(
            session=session, next_mode="readonly", persist_default_mode=False
        )
        assert session.mode == "readonly"
        assert "fs_write" not in set(session.tools)
        chat_loop_mod._apply_chat_effective_mode(
            session=session, next_mode="review", persist_default_mode=False
        )
        assert session.mode == "review"
        assert "fs_write" in set(session.tools)
    finally:
        session.close()
