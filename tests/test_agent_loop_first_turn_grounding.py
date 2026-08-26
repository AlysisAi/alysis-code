from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code.agent.prompt_context import _WorkspaceGroundingDescriptor
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface

SMOKE_MODEL = "gpt-4o-mini"


class _ScriptedClient:
    model = SMOKE_MODEL
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.call_records: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = tool_choice, stream, on_text_delta, temperature
        self.call_records.append({"messages": list(messages), "tools": tools})
        if len(self.call_records) > len(self._responses):
            raise AssertionError("scripted responses exhausted")
        return self._responses[len(self.call_records) - 1]


def _grounded_interactive_session(tmp_path: Path, *, session_id: str, client: _ScriptedClient):
    (tmp_path / "README.md").write_text("# Demo project\n", encoding="utf-8")
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override=session_id,
        surface=NoopSurface(),
    )
    session.client = client  # type: ignore[assignment]
    session.store.workspace_kind = "git_repo"
    session.workspace_grounding = _WorkspaceGroundingDescriptor(
        workspace_kind="git_repo",
        focus_relpath=".",
        stable_grounding_available=True,
        grounding_source="repo_scan",
        workspace_hint="Demo project",
        repo_summary_available=True,
        readme_available=True,
        manifest_available=True,
        conventions_available=False,
        anchor_paths=("README.md",),
    )
    return session


def _event_payloads(log_path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        event.get("payload") or {}
        for event in read_session_events(log_path)
        if event.get("type") == event_type
    ]


def test_first_turn_grounding_does_not_retry_conversational_answers(tmp_path: Path) -> None:
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Hi! What would you like to work on?",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session = _grounded_interactive_session(tmp_path, session_id="hi-no-retry", client=client)
    try:
        exit_code = session.run_turn("hi")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    # One LLM call, no forced repo inspection, no second greeting.
    assert len(client.call_records) == 1
    finals = _event_payloads(log_path, "final")
    assert [event.get("content") for event in finals] == ["Hi! What would you like to work on?"]


def test_first_turn_grounding_does_not_retry_greek_text(tmp_path: Path) -> None:
    handback = "Θες να ξεκινήσουμε με το κύριο αρχείο;"
    client = _ScriptedClient([LLMResponse(content=handback, tool_calls=[], raw={})])
    session = _grounded_interactive_session(
        tmp_path,
        session_id="hi-greek-no-retry",
        client=client,
    )
    try:
        exit_code = session.run_turn("γεια")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.call_records) == 1
    finals = _event_payloads(log_path, "final")
    assert [event.get("content") for event in finals] == [handback]


def test_first_turn_grounding_does_not_retry_arabic_text(tmp_path: Path) -> None:
    handback = "هل تريد البدء بمراجعة الملف الرئيسي؟"
    client = _ScriptedClient([LLMResponse(content=handback, tool_calls=[], raw={})])
    session = _grounded_interactive_session(
        tmp_path,
        session_id="hi-arabic-no-retry",
        client=client,
    )
    try:
        exit_code = session.run_turn("مرحبا")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.call_records) == 1
    finals = _event_payloads(log_path, "final")
    assert [event.get("content") for event in finals] == [handback]


def test_first_turn_grounding_does_not_retry_declarative_repo_answers(
    tmp_path: Path,
) -> None:
    first_reply = "The rate limiter is implemented in src/limiter.py using a token bucket."
    client = _ScriptedClient([LLMResponse(content=first_reply, tool_calls=[], raw={})])
    session = _grounded_interactive_session(tmp_path, session_id="declarative-retry", client=client)
    try:
        exit_code = session.run_turn("is there a rate limiter in this repo?")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.call_records) == 1
    finals = _event_payloads(log_path, "final")
    assert [event.get("content") for event in finals] == [first_reply]
