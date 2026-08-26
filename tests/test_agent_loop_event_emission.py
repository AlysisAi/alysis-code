from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

import pytest
from rich.console import Console

from alysis_code.agent.cache_keepalive import ParentCacheKeepalive
from alysis_code.agent_loop import AgentSession, ToolDef
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.config import AppConfig
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.llm.metadata import (
    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
    PROVIDER_METADATA_KEY,
)
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.model_registry import ModelMeta
from alysis_code.session_store import SessionStore, read_session_events
from alysis_code.subagents import SubagentDefinition, built_in_subagents
from alysis_code.surface.events import (
    Event,
    InfoEmitted,
    MessageDelta,
    MessageEnd,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
)
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.usage_tracker import UsageSummary


class _FakeRegistry:
    def get(self, model_name: str) -> ModelMeta:
        return ModelMeta(
            model_name=model_name,
            context_window_tokens=8192,
            max_output_tokens=2048,
            input_cost_per_token=None,
            output_cost_per_token=None,
            raw_metadata={},
            source="fallback",
        )


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        stream_chunks: list[list[str]] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._stream_chunks = list(stream_chunks or [])
        self.calls = 0
        self.requests: list[dict[str, Any]] = []
        self.call_messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
        on_text_delta: Any = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        _ = messages, tools, tool_choice, response_format, temperature, max_tokens
        call_index = self.calls
        self.calls += 1
        self.call_messages.append(list(messages))
        self.requests.append({"stream": stream, "has_delta_callback": callable(on_text_delta)})
        if stream and callable(on_text_delta) and call_index < len(self._stream_chunks):
            for chunk in self._stream_chunks[call_index]:
                on_text_delta(chunk)
        return self._responses[min(call_index, len(self._responses) - 1)]


class _KeepaliveAwareScriptedClient(_ScriptedClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(responses)
        self.keepalive_requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        if kwargs.get("max_tokens") == 16:
            self.keepalive_requests.append(dict(kwargs))
            return LLMResponse(content="ignored", tool_calls=[], raw={})
        kwargs.pop("cancellation_token", None)
        return super().chat(**kwargs)


class _RecordingEventSurface(NoopSurface):
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.legacy_tokens: list[str] = []
        self.legacy_assistant_done: list[str] = []
        self.legacy_tool_starts: list[Any] = []
        self.legacy_tool_outputs: list[Any] = []
        self.legacy_tool_ends: list[Any] = []

    def on_assistant_token(self, delta: str) -> None:
        self.legacy_tokens.append(delta)

    def on_assistant_message_done(self, text: str) -> None:
        self.legacy_assistant_done.append(text)

    def on_tool_start(self, event: Any) -> None:
        self.legacy_tool_starts.append(event)

    def on_tool_output(self, event: Any) -> None:
        self.legacy_tool_outputs.append(event)

    def on_tool_end(self, event: Any) -> None:
        self.legacy_tool_ends.append(event)

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(MessageDelta(text=text, worker_id=worker_id, role=role))

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(MessageEnd(text=text, worker_id=worker_id, role=role))

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(InfoEmitted(message=message, worker_id=worker_id, role=role))

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(
            ToolCallStarted(
                call_id=call_id,
                name=name,
                arguments_preview=arguments_preview,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(
            ToolCallProgress(call_id=call_id, text=text, worker_id=worker_id, role=role)
        )

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(
            ToolCallCompleted(
                call_id=call_id,
                success=success,
                result_preview=result_preview,
                worker_id=worker_id,
                role=role,
            )
        )


def _make_store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="s1",
        cwd=str(root),
        repo_root=str(root),
    )


def _make_session(
    *,
    root: Path,
    client: _ScriptedClient,
    surface: _RecordingEventSurface,
    stream: bool = False,
    tool: ToolDef | None = None,
    subagent_registry: dict[str, SubagentDefinition] | None = None,
    cfg: AppConfig | None = None,
) -> AgentSession:
    tools = {} if tool is None else {tool.name: tool}
    tool_list = [] if tool is None else [tool.as_openai_tool()]
    return AgentSession(
        subagent_registry=subagent_registry,
        cfg=cfg or AppConfig(model="test-model", routing_mode="code_only", stream=stream),
        root=root,
        mode="auto",
        yes=True,
        stream=stream,
        routing_mode="code_only",
        max_steps=4,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=_make_store(root),
        client=client,  # type: ignore[arg-type]
        model_registry=_FakeRegistry(),  # type: ignore[arg-type]
        usage_summary=UsageSummary(),
        usage_role="main",
        tool_output_offloader=None,
        conversation_compactor=None,
        tool_output_offload_enabled=False,
        conversation_summarization_enabled=False,
        compaction_profile="chat",
        tools=tools,
        tool_list=tool_list,
        messages=[{"role": "system", "content": "system prompt"}],
        verification_enabled=False,
        skills_enabled=False,
    )


def test_run_turn_streaming_assistant_text_emits_events_and_legacy(tmp_path: Path) -> None:
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [LLMResponse(content="Hello world.", tool_calls=[], raw={})],
        stream_chunks=[["Hello ", "world."]],
    )
    session = _make_session(root=tmp_path, client=client, surface=surface, stream=True)

    try:
        exit_code = session.run_turn("say hello")
    finally:
        session.close()

    assert exit_code == 0
    deltas = [event for event in surface.events if isinstance(event, MessageDelta)]
    ends = [event for event in surface.events if isinstance(event, MessageEnd)]
    assert [event.text for event in deltas] == ["Hello ", "world."]
    assert len(ends) == 1
    assert ends[0].text == "Hello world."
    assert "".join(event.text for event in deltas) == surface.legacy_assistant_done[-1]
    assert surface.legacy_tokens == ["Hello ", "world."]


def test_consecutive_turn_requests_keep_stable_history_before_volatile_suffix(
    tmp_path: Path,
) -> None:
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(content="First report.", tool_calls=[], raw={}),
            LLMResponse(content="Second report.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(root=tmp_path, client=client, surface=surface)
    session.messages.extend(
        [
            {"role": "user", "content": "<workspace_binding_context>\nroot: one\n"},
            {"role": "user", "content": "<task_brief>\ncurrent: one\n"},
            {"role": "user", "content": "<environment_context>\nmode: auto\n"},
        ]
    )

    try:
        assert (
            session.run_turn(
                "First task.",
                ephemeral_system_messages=["turn-system-one"],
                ephemeral_user_messages=["turn-user-one"],
            )
            == 0
        )
        workspace_message = next(
            message
            for message in session.messages
            if str(message.get("content") or "").startswith("<workspace_binding_context>")
        )
        workspace_message["content"] = "<workspace_binding_context>\nroot: two\n"
        assert (
            session.run_turn(
                "Second task.",
                ephemeral_system_messages=["turn-system-two"],
                ephemeral_user_messages=["turn-user-two"],
            )
            == 0
        )
    finally:
        session.close()

    assert len(client.call_messages) == 2
    first, second = client.call_messages
    volatile_markers = (
        "<workspace_binding_context>",
        "<task_brief>",
        "<environment_context>",
        "turn-system-",
        "turn-user-",
    )

    def stable_prefix(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        first_volatile = next(
            (
                index
                for index, message in enumerate(messages)
                if str(message.get("content") or "").startswith(volatile_markers)
            ),
            len(messages),
        )
        return messages[:first_volatile]

    first_stable = stable_prefix(first)
    second_stable = stable_prefix(second)
    assert second_stable[: len(first_stable)] == first_stable
    assert (
        json.dumps(
            second_stable[: len(first_stable)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        == json.dumps(
            first_stable,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    assert any(message.get("content") == "turn-system-two" for message in second)
    assert any(message.get("content") == "turn-user-two" for message in second)
    suffix_start = len(second_stable)
    second_contents = [str(message.get("content") or "") for message in second]
    for expected in ("First task.", "Second task."):
        assert expected in second_contents
        assert second_contents.index(expected) < suffix_start


def test_session_close_persists_cache_efficiency_summary(tmp_path: Path) -> None:
    surface = _RecordingEventSurface()
    client = _ScriptedClient([LLMResponse(content="Done.", tool_calls=[], raw={})])
    session = _make_session(root=tmp_path, client=client, surface=surface)
    session.store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "sessions",
        session_id="cache-summary",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    session.usage_summary.add_event_payload(
        {
            "event_type": "llm_usage",
            "timestamp": "2026-08-20T00:00:01Z",
            "role": "main",
            "requested_model": "test-model",
            "prompt_tokens": 100,
            "completion_tokens": 1,
            "total_tokens": 101,
            "cached_prompt_tokens": 75,
            "uncached_prompt_tokens": 25,
            "usage_source": "api",
        }
    )

    session.close()

    events = read_session_events(tmp_path / "sessions" / "cache-summary.jsonl")
    summaries = [
        event["payload"] for event in events if event["type"] == "cache_efficiency_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["hit_ratio"] == 0.75
    assert summaries[0]["largest_uncached_call"]["call_position"] == 1


def test_run_turn_streaming_tool_call_executes_after_stream_finishes(tmp_path: Path) -> None:
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": args["message"]},
    )
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(
                content="I will call the tool.",
                tool_calls=[
                    ToolCall(id="call_streamed", name="echo_tool", arguments={"message": "hello"})
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ],
        stream_chunks=[["I will ", "call the tool."]],
    )
    session = _make_session(root=tmp_path, client=client, surface=surface, stream=True, tool=tool)

    try:
        exit_code = session.run_turn("run the tool")
    finally:
        session.close()

    assert exit_code == 0
    assert client.requests[0] == {"stream": True, "has_delta_callback": True}
    assert client.requests[1] == {"stream": True, "has_delta_callback": True}
    event_types = [type(event) for event in surface.events]
    first_tool_index = event_types.index(ToolCallStarted)
    assert event_types[:first_tool_index] == [MessageDelta, MessageDelta, MessageEnd]
    assert [event.text for event in surface.events[:2]] == ["I will ", "call the tool."]


def test_run_turn_streaming_provider_metadata_survives_tool_followup_request(
    tmp_path: Path,
) -> None:
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": args["message"]},
    )
    provider_content = {
        "role": "model",
        "parts": [
            {"text": "I will call the tool."},
            {
                "functionCall": {
                    "id": "call_streamed",
                    "name": "echo_tool",
                    "args": {"message": "hello"},
                },
                "thoughtSignature": "streamed-thought-signature",
            },
        ],
    }
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(
                content="I will call the tool.",
                tool_calls=[
                    ToolCall(
                        id="call_streamed",
                        name="echo_tool",
                        arguments={"message": "hello"},
                        provider_metadata={
                            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                                "part_index": 1,
                                "thoughtSignature": "streamed-thought-signature",
                            }
                        },
                    )
                ],
                raw={},
                provider_metadata={
                    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                        "content": provider_content,
                    }
                },
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ],
        stream_chunks=[["I will ", "call the tool."]],
    )
    session = _make_session(root=tmp_path, client=client, surface=surface, stream=True, tool=tool)

    try:
        exit_code = session.run_turn("run the tool")
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.call_messages) >= 2
    followup_assistant = next(
        message
        for message in client.call_messages[1]
        if str(message.get("role")) == "assistant" and message.get("tool_calls")
    )
    assert (
        followup_assistant[PROVIDER_METADATA_KEY][GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY][
            "content"
        ]
        == provider_content
    )
    assert (
        followup_assistant[PROVIDER_METADATA_KEY]["_tool_calls"][0]["metadata"][
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY
        ]["thoughtSignature"]
        == "streamed-thought-signature"
    )


def test_run_turn_tool_success_emits_lifecycle_events_and_legacy(tmp_path: Path) -> None:
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": args["message"]},
    )
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call_1", name="echo_tool", arguments={"message": "hello"})
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(root=tmp_path, client=client, surface=surface, tool=tool)

    try:
        exit_code = session.run_turn("run the tool")
    finally:
        session.close()

    assert exit_code == 0
    tool_events = [
        event
        for event in surface.events
        if isinstance(event, ToolCallStarted | ToolCallProgress | ToolCallCompleted)
    ]
    assert [type(event) for event in tool_events] == [
        ToolCallStarted,
        ToolCallProgress,
        ToolCallCompleted,
    ]
    assert tool_events[0] == ToolCallStarted(
        call_id="call_1",
        name="echo_tool",
        arguments_preview='{"message": "hello"}',
    )
    assert isinstance(tool_events[2], ToolCallCompleted)
    assert tool_events[2].success is True
    assert len(surface.legacy_tool_starts) == 1
    assert len(surface.legacy_tool_outputs) == 1
    assert len(surface.legacy_tool_ends) == 1


def test_child_step_message_is_framed_after_prior_tool_result(tmp_path: Path) -> None:
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda _args: {"ok": True},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Checking first.",
                tool_calls=[ToolCall(id="call-1", name="echo_tool", arguments={})],
                raw={},
            ),
            LLMResponse(content="Done with the guidance.", tool_calls=[], raw={}),
        ]
    )
    surface = _RecordingEventSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
    )
    drain_count = 0

    def _drain_messages() -> list[str]:
        nonlocal drain_count
        drain_count += 1
        if drain_count == 2:
            return ["Message from the parent agent: Focus on the parser."]
        return []

    session.step_system_message_provider = _drain_messages
    try:
        assert session.run_turn("Inspect the code.") == 0
    finally:
        session.close()

    assert all(
        "Message from the parent agent:" not in str(message.get("content") or "")
        for message in client.call_messages[0]
    )
    followup = client.call_messages[1]
    tool_result_index = next(
        index for index, message in enumerate(followup) if message.get("role") == "tool"
    )
    parent_message_index = next(
        index
        for index, message in enumerate(followup)
        if message.get("content") == "Message from the parent agent: Focus on the parser."
    )
    assert followup[parent_message_index]["role"] == "system"
    assert parent_message_index > tool_result_index


def test_protocol_surface_real_turn_emits_single_canonical_lifecycle(tmp_path: Path) -> None:
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": args["message"]},
    )
    envelopes: list[dict[str, Any]] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1", job_id="job-1"),
        emit=envelopes.append,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_tool", arguments={"message": "hi"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,  # type: ignore[arg-type]
        tool=tool,
    )

    try:
        exit_code = session.run_turn("run the tool")
    finally:
        session.close()

    assert exit_code == 0
    lifecycle = [
        envelope
        for envelope in envelopes
        if envelope["type"] in {"tool_call_started", "tool_call_progress", "tool_call_completed"}
    ]
    assert [envelope["type"] for envelope in lifecycle] == [
        "tool_call_started",
        "tool_call_progress",
        "tool_call_completed",
    ]
    assert lifecycle[-1]["payload"]["success"] is True
    assert len([envelope for envelope in envelopes if envelope["type"] == "message_end"]) == 1


def test_run_turn_dispatches_same_batch_subagent_runs_in_parallel(tmp_path: Path) -> None:
    # Generous rendezvous timeout: only reached when dispatch is broken (serial),
    # so a large value costs nothing on the happy path but absorbs arbitrary
    # scheduler delays on loaded CI runners.
    barrier_timeout = 30.0
    start_times: dict[str, float] = {}
    end_times: dict[str, float] = {}
    rendezvous_results: dict[str, bool] = {}
    lock = threading.Lock()
    both_started = threading.Event()

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        task = str(args["task"])
        with lock:
            start_times[task] = perf_counter()
            if len(start_times) == 2:
                both_started.set()
        # Rendezvous: released only once BOTH subagents have started. If the
        # batch were dispatched serially, the first call would block here for
        # the full timeout (the sibling cannot start until this call returns)
        # and record False, deterministically failing the assertion below.
        reached_rendezvous = both_started.wait(timeout=barrier_timeout)
        with lock:
            rendezvous_results[task] = reached_rendezvous
            end_times[task] = perf_counter()
        return {
            "subagent": "explorer",
            "subagent_session_id": f"sub-{task}",
            "result": f"catalog for {task}",
        }

    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_a",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "alpha"},
                    ),
                    ToolCall(
                        id="call_b",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "beta"},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        # Parallel prelaunch of a same-batch subagent group requires every call
        # to resolve an exactly-readonly subagent. Real sessions always carry a
        # resolved registry (built-ins at minimum), so mirror that here to make
        # "explorer" resolve to its built-in readonly definition.
        subagent_registry=built_in_subagents(),
    )

    try:
        exit_code = session.run_turn("catalog two areas")
    finally:
        session.close()

    assert exit_code == 0
    assert set(start_times) == {"alpha", "beta"}
    assert set(end_times) == {"alpha", "beta"}
    # Primary parallelism proof: every subagent saw its sibling start before it
    # finished. This is scheduling-independent — no wall-clock comparison.
    assert rendezvous_results == {"alpha": True, "beta": True}, (
        "same-batch subagent_run calls were not dispatched concurrently: each "
        f"fake subagent waited up to {barrier_timeout}s for its sibling to start, "
        f"but the rendezvous never completed (rendezvous_results="
        f"{rendezvous_results}, start_times={start_times}, end_times={end_times})"
    )
    # Secondary sanity check: the recorded windows must overlap (both runs
    # started before either finished). Guaranteed by the rendezvous above;
    # <= tolerates identical perf_counter readings.
    assert max(start_times.values()) <= min(end_times.values())

    tool_messages = [
        message for message in client.call_messages[1] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_a", "call_b"]
    assert "catalog for alpha" in tool_messages[0]["content"]
    assert "catalog for beta" in tool_messages[1]["content"]


def test_parallel_subagent_tool_duration_uses_child_elapsed_time(tmp_path: Path) -> None:
    child_durations = {"alpha": 3_210, "beta": 6_540}

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        task = str(args["task"])
        return {
            "subagent": "explorer",
            "subagent_session_id": f"sub-{task}",
            "result": f"catalog for {task}",
            "elapsed_ms": child_durations[task],
        }

    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call_{task}",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": task},
                    )
                    for task in child_durations
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        subagent_registry=built_in_subagents(),
    )

    try:
        assert session.run_turn("catalog two areas") == 0
    finally:
        session.close()

    assert [(event.tool_call_id, event.elapsed_ms) for event in surface.legacy_tool_ends] == [
        ("call_alpha", 3_210),
        ("call_beta", 6_540),
    ]


def test_sequential_subagent_tool_duration_ignores_child_elapsed_time(tmp_path: Path) -> None:
    reported_child_duration = 987_654
    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda _args: {
            "subagent": "explorer",
            "subagent_session_id": "sub-alpha",
            "result": "catalog for alpha",
            "elapsed_ms": reported_child_duration,
        },
    )
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_alpha",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "alpha"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        subagent_registry=built_in_subagents(),
    )

    try:
        assert session.run_turn("catalog one area") == 0
    finally:
        session.close()

    assert len(surface.legacy_tool_ends) == 1
    assert surface.legacy_tool_ends[0].elapsed_ms != reported_child_duration


def test_sync_subagent_wait_refreshes_parent_cache_after_idle_threshold(tmp_path: Path) -> None:
    tool = ToolDef(
        name="subagent_run",
        description="slow fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda _args: (
            sleep(0.08)
            or {
                "subagent": "explorer",
                "subagent_session_id": "slow-child",
                "result": "mapped",
            }
        ),
    )
    client = _KeepaliveAwareScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_slow",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "map the repository"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingEventSurface(),
        tool=tool,
        subagent_registry=built_in_subagents(),
    )
    session.cache_keepalive = ParentCacheKeepalive(
        enabled=True,
        idle_threshold_s=0.02,
        send_ping=session._send_cache_keepalive,
        deadline=None,
    )

    try:
        assert session.run_turn("map with a child") == 0
    finally:
        session.close()

    assert client.keepalive_requests
    first = client.keepalive_requests[0]
    assert first["messages"][0]["content"] == "system prompt"
    assert first["max_tokens"] == 16
    assert first["temperature"] == 0.0
    assert first["stream"] is False


def test_parallel_subagent_batch_caps_active_workers_at_four(tmp_path: Path) -> None:
    active = 0
    max_active = 0
    started: list[str] = []
    lock = threading.Lock()
    first_wave_started = threading.Event()
    release_first_wave = threading.Event()

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        task = str(args["task"])
        with lock:
            active += 1
            max_active = max(max_active, active)
            started.append(task)
            if len(started) == 4:
                first_wave_started.set()
        assert release_first_wave.wait(timeout=30.0)
        with lock:
            active -= 1
        return {
            "subagent": "explorer",
            "subagent_session_id": f"sub-{task}",
            "result": f"catalog for {task}",
        }

    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call_{index}",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": f"area-{index}"},
                    )
                    for index in range(5)
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    surface = _RecordingEventSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        subagent_registry=built_in_subagents(),
    )
    outcome: list[BaseException | int] = []

    def _run_parent() -> None:
        try:
            outcome.append(session.run_turn("catalog five areas"))
        except BaseException as exc:  # noqa: BLE001 - preserve worker failure for assertion
            outcome.append(exc)

    worker = threading.Thread(target=_run_parent, daemon=True)
    worker.start()
    try:
        assert first_wave_started.wait(timeout=5.0)
        with lock:
            assert len(started) == 4
            assert active == 4
            assert max_active == 4
        release_first_wave.set()
        worker.join(timeout=5.0)
    finally:
        release_first_wave.set()
        worker.join(timeout=5.0)
        session.close()

    assert not worker.is_alive()
    assert outcome == [0]
    assert sorted(started) == [f"area-{index}" for index in range(5)]
    assert max_active == 4
    tool_messages = [
        message for message in client.call_messages[1] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        f"call_{index}" for index in range(5)
    ]
    assert not [event for event in surface.events if isinstance(event, InfoEmitted)]


def test_mixed_subagent_batch_runs_eligible_calls_first_and_preserves_result_order(
    tmp_path: Path,
) -> None:
    active = 0
    max_active = 0
    started: list[str] = []
    finished: list[str] = []
    lock = threading.Lock()
    all_parallel_started = threading.Event()
    deferred_started = threading.Event()
    releases = {f"area-{index}": threading.Event() for index in range(4)}
    completions = {f"area-{index}": threading.Event() for index in range(4)}

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        task = str(args["task"])
        if task == "deferred":
            with lock:
                assert finished == ["area-3", "area-2", "area-1", "area-0"]
                assert active == 0
            deferred_started.set()
        else:
            with lock:
                active += 1
                max_active = max(max_active, active)
                started.append(task)
                if len(started) == 4:
                    all_parallel_started.set()
            assert releases[task].wait(timeout=30.0)
            with lock:
                finished.append(task)
                active -= 1
            completions[task].set()
        return {
            "subagent": str(args["name"]),
            "subagent_session_id": f"sub-{task}",
            "result": f"result for {task}",
        }

    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    calls = [
        ToolCall(
            id="call_deferred",
            name="subagent_run",
            arguments={"name": "implementer", "task": "deferred", "mode": "review"},
        ),
        *[
            ToolCall(
                id=f"call_{index}",
                name="subagent_run",
                arguments={"name": "explorer", "task": f"area-{index}"},
            )
            for index in range(4)
        ],
    ]
    client = _ScriptedClient(
        [
            LLMResponse(content="", tool_calls=calls, raw={}),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        subagent_registry=built_in_subagents(),
    )
    outcome: list[BaseException | int] = []

    def _run_parent() -> None:
        try:
            outcome.append(session.run_turn("review five areas"))
        except BaseException as exc:  # noqa: BLE001 - preserve worker failure
            outcome.append(exc)

    worker = threading.Thread(target=_run_parent, daemon=True)
    worker.start()
    try:
        assert all_parallel_started.wait(timeout=5.0)
        assert not deferred_started.is_set()
        for index in reversed(range(4)):
            task = f"area-{index}"
            releases[task].set()
            assert completions[task].wait(timeout=5.0)
        assert deferred_started.wait(timeout=5.0)
        worker.join(timeout=5.0)
    finally:
        for release in releases.values():
            release.set()
        worker.join(timeout=5.0)
        session.close()

    assert not worker.is_alive()
    assert outcome == [0]
    assert sorted(started) == [f"area-{index}" for index in range(4)]
    assert max_active == 4
    tool_messages = [
        message for message in client.call_messages[1] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_deferred",
        "call_0",
        "call_1",
        "call_2",
        "call_3",
    ]
    assert [json.loads(message["content"])["result"] for message in tool_messages] == [
        "result for deferred",
        "result for area-0",
        "result for area-1",
        "result for area-2",
        "result for area-3",
    ]
    notices = [text for role, text in transcript.entries if role == "info"]
    assert notices == [
        "Running 1 of 5 subagents one at a time: shared workspace can write (implementer)"
    ]
    serialized_events = [
        event
        for event in session.store.events_snapshot()
        if event["type"] == "subagent_batch_serialized"
    ]
    assert len(serialized_events) == 1
    assert serialized_events[0]["payload"] == {
        "eligible": 4,
        "deferred": 1,
        "reason": "shared workspace can write",
        "run_ids": [],
        "deferred_roles": ["implementer"],
    }


def test_mixed_subagent_batch_cancellation_prevents_deferred_call(
    tmp_path: Path,
) -> None:
    class _CancellationToken:
        def __init__(self) -> None:
            self._cancelled = threading.Event()

        @property
        def is_cancelled(self) -> bool:
            return self._cancelled.is_set()

        def cancel(self) -> None:
            self._cancelled.set()

        def throw_if_cancelled(self, reason: str = "cancelled_by_user") -> None:
            if self.is_cancelled:
                raise KeyboardInterrupt(reason)

    started = 0
    started_lock = threading.Lock()
    parallel_started = threading.Event()
    release_parallel = threading.Event()
    deferred_started = threading.Event()

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal started
        task = str(args["task"])
        if task == "deferred":
            deferred_started.set()
        else:
            with started_lock:
                started += 1
                if started == 2:
                    parallel_started.set()
            assert release_parallel.wait(timeout=30.0)
        return {
            "subagent": str(args["name"]),
            "subagent_session_id": f"sub-{task}",
            "result": f"result for {task}",
        }

    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_deferred",
                        name="subagent_run",
                        arguments={
                            "name": "implementer",
                            "task": "deferred",
                            "mode": "review",
                        },
                    ),
                    ToolCall(
                        id="call_a",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "alpha"},
                    ),
                    ToolCall(
                        id="call_b",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "beta"},
                    ),
                ],
                raw={},
            )
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingEventSurface(),
        tool=tool,
        subagent_registry=built_in_subagents(),
    )
    token = _CancellationToken()
    outcome: list[BaseException | int] = []

    def _run_parent() -> None:
        try:
            outcome.append(session.run_turn("review three areas", cancellation_token=token))
        except BaseException as exc:  # noqa: BLE001 - cancellation is the assertion
            outcome.append(exc)

    worker = threading.Thread(target=_run_parent, daemon=True)
    worker.start()
    try:
        assert parallel_started.wait(timeout=5.0)
        token.cancel()
        worker.join(timeout=5.0)
    finally:
        release_parallel.set()
        worker.join(timeout=5.0)
        session.close()

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], KeyboardInterrupt)
    assert not deferred_started.is_set()


def test_nonwriting_shared_batch_mutation_is_reported_as_batch_failure(
    tmp_path: Path,
) -> None:
    both_started = threading.Barrier(2)
    deferred_started = threading.Event()

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args["name"])
        if name == "implementer":
            deferred_started.set()
            return {
                "subagent": name,
                "subagent_session_id": "implement-session",
                "result": "unexpected deferred result",
            }
        both_started.wait(timeout=5.0)
        if name == "debugger":
            return {
                "error": "debugger modified the workspace",
                "error_code": "unexpected_workspace_mutation",
                "failure_category": "workspace_mutation",
                "status": "degraded",
                "subagent": name,
                "subagent_session_id": "debug-session",
                "material_touched_repo_paths": ["src/oops.py"],
            }
        return {
            "subagent": name,
            "subagent_session_id": "verify-session",
            "result": "verification complete",
        }

    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_implement",
                        name="subagent_run",
                        arguments={
                            "name": "implementer",
                            "task": "do not start",
                            "mode": "review",
                        },
                    ),
                    ToolCall(
                        id="call_debug",
                        name="subagent_run",
                        arguments={"name": "debugger", "task": "diagnose"},
                    ),
                    ToolCall(
                        id="call_verify",
                        name="subagent_run",
                        arguments={"name": "verifier", "task": "verify"},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    cfg.subagent_orchestration.parallel_nonwriting_shared = True
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingEventSurface(),
        tool=tool,
        subagent_registry=built_in_subagents(),
        cfg=cfg,
    )

    try:
        assert session.run_turn("diagnose and verify") == 0
    finally:
        session.close()

    tool_messages = [
        message for message in client.call_messages[1] if message.get("role") == "tool"
    ]
    results = [json.loads(str(message["content"])) for message in tool_messages]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_implement",
        "call_debug",
        "call_verify",
    ]
    assert results[0]["error_code"] == "parallel_subagent_batch_workspace_mutation"
    assert results[0]["batch_failure"] is True
    assert results[0]["deferred_call_not_started"] is True
    assert results[0]["offending_runs"] == [
        {
            "run_id": "debug-session",
            "subagent": "debugger",
            "material_touched_repo_paths": ["src/oops.py"],
        }
    ]
    assert results[1]["error_code"] == "parallel_subagent_batch_workspace_mutation"
    assert results[2]["result"] == "verification complete"
    assert not deferred_started.is_set()


def test_parallel_subagent_failure_preserves_successful_sibling_result(tmp_path: Path) -> None:
    both_started = threading.Barrier(2)

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        task = str(args["task"])
        both_started.wait(timeout=30.0)
        if task == "alpha":
            raise RuntimeError("alpha failed")
        return {
            "subagent": "explorer",
            "subagent_session_id": "sub-beta",
            "result": "catalog for beta",
        }

    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_a",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "alpha"},
                    ),
                    ToolCall(
                        id="call_b",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "beta"},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingEventSurface(),
        tool=tool,
        subagent_registry=built_in_subagents(),
    )

    try:
        exit_code = session.run_turn("catalog two areas")
    finally:
        session.close()

    assert exit_code == 0
    tool_messages = [
        message for message in client.call_messages[1] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_a", "call_b"]
    assert "alpha failed" in tool_messages[0]["content"]
    assert "catalog for beta" in tool_messages[1]["content"]


def test_run_turn_serializes_same_batch_review_subagents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_order: list[str] = []

    def _run_subagent(args: dict[str, Any]) -> dict[str, Any]:
        task = str(args["task"])
        execution_order.append(f"start:{task}")
        execution_order.append(f"end:{task}")
        return {
            "subagent": "implementer",
            "subagent_session_id": f"sub-{task}",
            "result": f"result for {task}",
        }

    class _UnexpectedParallelExecutor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            raise AssertionError("review-mode subagents must not use the parallel executor")

    monkeypatch.setattr(
        "alysis_code.agent.turn.core.ThreadPoolExecutor",
        _UnexpectedParallelExecutor,
    )
    tool = ToolDef(
        name="subagent_run",
        description="fake subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_subagent,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_a",
                        name="subagent_run",
                        arguments={
                            "name": "implementer",
                            "task": "alpha",
                            "mode": "review",
                        },
                    ),
                    ToolCall(
                        id="call_b",
                        name="subagent_run",
                        arguments={
                            "name": "frontend-engineer",
                            "task": "beta",
                            "mode": "review",
                        },
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingEventSurface(),
        tool=tool,
        subagent_registry=built_in_subagents(),
    )

    try:
        exit_code = session.run_turn("review two changes")
    finally:
        session.close()

    assert exit_code == 0
    assert execution_order == ["start:alpha", "end:alpha", "start:beta", "end:beta"]


def test_run_turn_tool_failure_emits_failed_completion_event(tmp_path: Path) -> None:
    def _fail_tool(args: dict[str, Any]) -> dict[str, Any]:
        _ = args
        raise RuntimeError("boom")

    tool = ToolDef(
        name="explode_tool",
        description="explode",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_fail_tool,
    )
    surface = _RecordingEventSurface()
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="explode_tool", arguments={})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(root=tmp_path, client=client, surface=surface, tool=tool)

    try:
        exit_code = session.run_turn("run the failing tool")
    finally:
        session.close()

    assert exit_code == 0
    completed = [event for event in surface.events if isinstance(event, ToolCallCompleted)]
    assert len(completed) == 1
    assert completed[0].success is False
    assert "boom" in completed[0].result_preview
    assert surface.legacy_tool_ends[0].status == "failed"
