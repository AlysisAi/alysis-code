from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.protocols import ReasoningTraceCapability
from alysis_code.llm.types import ReasoningOutputKind
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.tools.registry import build_unknown_tool_recovery_payload


class _FakeClient:
    model = "test-model"
    temperature = 0.2
    reasoning_trace_capability = ReasoningTraceCapability(
        adapter="test_safe_summary",
        output_kind=ReasoningOutputKind.SUMMARY,
        supports_streaming=True,
    )

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        reasoning_chunks: list[list[str]] | None = None,
    ) -> None:
        self._responses = responses
        self._reasoning_chunks = list(reasoning_chunks or [])
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        on_reasoning_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, on_text_delta, temperature
        call_index = self.calls
        response = self._responses[call_index]
        self.calls += 1
        if stream and callable(on_reasoning_delta) and call_index < len(self._reasoning_chunks):
            for chunk in self._reasoning_chunks[call_index]:
                on_reasoning_delta(chunk)
        return response


class _ReasoningSurface(NoopSurface):
    def __init__(self) -> None:
        self.reasoning_tokens: list[str] = []

    def on_reasoning_token(self, token: str) -> None:
        self.reasoning_tokens.append(token)


def _session(tmp_path: Path, *, session_id: str):
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override=session_id,
    )


def _tool_results(tmp_path: Path, session_id: str) -> list[dict[str, Any]]:
    events = list(read_session_events(tmp_path / "sessions" / f"{session_id}.jsonl"))
    return [event.get("payload", {}) for event in events if event.get("type") == "tool_result"]


def test_unknown_tool_returns_available_names_and_nearest_suggestions(tmp_path: Path) -> None:
    session_id = "unknown-tool-suggestion"
    session = _session(tmp_path, session_id=session_id)
    session.client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_reed", arguments={"path": "README.md"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        assert session.run_turn("Read README.md.") == 0
    finally:
        session.close()

    result = _tool_results(tmp_path, session_id)[0]["result"]
    assert result["error_code"] == "unknown_tool"
    assert result["requested_tool_name"] == "fs_reed"
    assert "fs_read" in result["available_tool_names"]
    assert "fs_read" in result["nearest_tool_suggestions"]
    assert result["safe_compatibility_alias"] is False


def test_registered_schema_compatible_alias_executes_target_tool(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello alias\n", encoding="utf-8", newline="\n")
    session_id = "unknown-tool-safe-alias"
    session = _session(tmp_path, session_id=session_id)
    session.client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="read_file", arguments={"path": "README.md"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        assert session.run_turn("Read README.md.") == 0
    finally:
        session.close()

    payload = _tool_results(tmp_path, session_id)[0]
    assert payload["executed_tool_name"] == "fs_read"
    assert payload["compatibility_alias"]["alias"] == "read_file"
    assert payload["result"]["content"] == "hello alias\n"


def test_ambiguous_alias_does_not_execute_any_tool(tmp_path: Path) -> None:
    session_id = "unknown-tool-ambiguous"
    session = _session(tmp_path, session_id=session_id)
    session.client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="read", arguments={"path": "README.md"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        assert session.run_turn("Read README.md.") == 0
    finally:
        session.close()

    result = _tool_results(tmp_path, session_id)[0]["result"]
    assert result["error_code"] == "unknown_tool"
    assert result["alias_ambiguous"] is True
    assert "content" not in result


def test_repeated_identical_unknown_tool_hits_retry_guard(tmp_path: Path) -> None:
    session_id = "unknown-tool-repeat-guard"
    session = _session(tmp_path, session_id=session_id)
    repeated = ToolCall(id="tc", name="fs_reed", arguments={"path": "README.md"})
    session.client = _FakeClient(
        [
            LLMResponse(content="", tool_calls=[repeated], raw={}),
            LLMResponse(content="", tool_calls=[repeated], raw={}),
            LLMResponse(content="", tool_calls=[repeated], raw={}),
            LLMResponse(content="", tool_calls=[repeated], raw={}),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        assert session.run_turn("Read README.md.") == 0
    finally:
        session.close()

    results = [payload["result"] for payload in _tool_results(tmp_path, session_id)]
    assert any("Blocked repeated tool call" in str(result.get("error")) for result in results)
    events = list(read_session_events(tmp_path / "sessions" / f"{session_id}.jsonl"))
    assert any(
        event.get("type") == "warning"
        and event.get("payload", {}).get("warning") == "repeated_tool_failure_guard"
        for event in events
    )


def test_custom_tool_names_appear_without_schema_or_secret_metadata() -> None:
    payload = build_unknown_tool_recovery_payload(
        requested_tool_name="secret_lookup",
        arguments={"token": "sk-test-secret-value"},
        available_tool_names=["fs_read", "custom_secret_lookup"],
    )

    assert "custom_secret_lookup" in payload["available_tool_names"]
    rendered = str(payload)
    assert "parameters" not in rendered
    assert "sk-test-secret-value" not in rendered


def test_unknown_tool_recovery_runs_inside_subagent_child_session(tmp_path: Path) -> None:
    session_id = "unknown-tool-subagent-child"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        runtime_kind=RuntimeKind.SUBAGENT,
        subagents_enabled=False,
        subagent_depth=1,
        session_log_dir_override=tmp_path / "sessions",
        session_id_override=session_id,
    )
    session.client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_reed", arguments={"path": "README.md"})],
                raw={},
            ),
            LLMResponse(content="subagent done", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        assert session.run_turn("Read README.md.") == 0
    finally:
        session.close()

    result = _tool_results(tmp_path, session_id)[0]["result"]
    assert result["error_code"] == "unknown_tool"
    assert result["requested_tool_name"] == "fs_reed"
    assert "fs_read" in result["available_tool_names"]
    assert "fs_read" in result["nearest_tool_suggestions"]


def test_streaming_subagent_child_session_forwards_reasoning(tmp_path: Path) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=True),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        runtime_kind=RuntimeKind.SUBAGENT,
        subagents_enabled=False,
        subagent_depth=1,
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="streaming-subagent-child-reasoning",
    )
    surface = _ReasoningSurface()
    session.surface = surface
    session.client = _FakeClient(
        [LLMResponse(content="subagent done", tool_calls=[], raw={})],
        reasoning_chunks=[["thinking ", "inside child"]],
    )  # type: ignore[assignment]

    try:
        assert session.run_turn("Read README.md.") == 0
    finally:
        session.close()

    assert "".join(surface.reasoning_tokens) == "thinking inside child"
