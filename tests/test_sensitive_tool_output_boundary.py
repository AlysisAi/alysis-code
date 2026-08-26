from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

from rich.console import Console

from alysis_code.agent.sensitive_output import (
    collect_sensitive_response_taints,
    redact_sensitive_response_for_persistence,
    redact_sensitive_response_taints,
    redact_sensitive_tool_arguments,
    redact_sensitive_tool_result,
    sensitive_tool_boundary,
)
from alysis_code.agent_loop import AgentSession, ToolDef
from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.config import AppConfig
from alysis_code.hooks.models import HookDispatchResult
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.model_registry import ModelMeta
from alysis_code.session_artifacts import SessionArtifactLayout
from alysis_code.session_store import SessionStore
from alysis_code.surface.events import (
    Event,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
)
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.usage_tracker import UsageSummary

_CANARY = "ALYSIS_SECRET_CANARY_7dc6f758"
_ERROR_CANARY = "ALYSIS_EXCEPTION_CANARY_c18afe42"


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

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0
        # Keep the host's request objects by reference. The runtime must scrub
        # the one-call-only sensitive material after the provider consumes it.
        self.request_references: list[list[dict[str, Any]]] = []
        self.saw_canary_during_call: list[bool] = []
        self.request_snapshots: list[str] = []
        self.stream_flags: list[bool] = []

    def chat(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.request_references.append(messages)
        request_snapshot = _serialized(messages)
        self.request_snapshots.append(request_snapshot)
        self.saw_canary_during_call.append(_CANARY in request_snapshot)
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        stream = bool(kwargs.get("stream"))
        self.stream_flags.append(stream)
        on_text_delta = kwargs.get("on_text_delta")
        if stream and callable(on_text_delta) and response.content:
            on_text_delta(response.content)
        return response


class _RecordingSurface(NoopSurface):
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.legacy_events: list[Any] = []

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(ToolCallStarted(call_id, name, arguments_preview, worker_id, role))

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(ToolCallProgress(call_id, text, worker_id, role))

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(ToolCallCompleted(call_id, success, result_preview, worker_id, role))

    def on_tool_start(self, event: Any) -> None:
        self.legacy_events.append(event)

    def on_tool_output(self, event: Any) -> None:
        self.legacy_events.append(event)

    def on_tool_end(self, event: Any) -> None:
        self.legacy_events.append(event)


class _RecordingHooks:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def fire_user_prompt_submit(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_pre_tool_use(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_post_tool_use(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_turn_complete(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_session_end(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()


class _RecordingDiagnostics:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event(self, event_type: str, payload: dict[str, Any], *, durable: bool = False) -> None:
        self.events.append(
            {"event_type": event_type, "payload": copy.deepcopy(payload), "durable": durable}
        )


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _make_session(
    tmp_path: Path,
    *,
    tool_result: dict[str, Any] | None = None,
    tool_error: Exception | None = None,
    final_response: str = "Done.",
    final_raw: dict[str, Any] | None = None,
    initial_response: str = f"Preparing {_CANARY}",
    stream: bool = False,
) -> tuple[AgentSession, Any]:
    def run_tool(_args: dict[str, Any]) -> dict[str, Any]:
        if tool_error is not None:
            raise tool_error
        return copy.deepcopy(tool_result or {})

    tool = ToolDef(
        name="fs_read",
        description="test sensitive read",
        parameters={"type": "object", "properties": {}, "required": []},
        run=run_tool,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content=initial_response,
                tool_calls=[
                    ToolCall(
                        id="call-sensitive",
                        name="fs_read",
                        arguments={"path": ".env", "content": _CANARY},
                    )
                ],
                raw={"tool_call": {"arguments": _CANARY}},
            ),
            LLMResponse(content=final_response, tool_calls=[], raw=final_raw or {}),
        ]
    )
    surface = _RecordingSurface()
    hooks = _RecordingHooks()
    diagnostics = _RecordingDiagnostics()
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "sessions",
        session_id="sensitive-boundary",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(tmp_path / "session-artifacts"),
        workspace_root=tmp_path,
        threshold_chars=1,
        preview_chars=1,
    )
    session = AgentSession(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=stream),
        root=tmp_path,
        mode="auto",
        yes=True,
        stream=stream,
        routing_mode="code_only",
        max_steps=4,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=store,
        client=client,  # type: ignore[arg-type]
        model_registry=_FakeRegistry(),  # type: ignore[arg-type]
        usage_summary=UsageSummary(),
        usage_role="main",
        tool_output_offloader=offloader,
        conversation_compactor=None,
        tool_output_offload_enabled=True,
        conversation_summarization_enabled=False,
        compaction_profile="chat",
        tools={tool.name: tool},
        tool_list=[tool.as_openai_tool()],
        messages=[{"role": "system", "content": "system prompt"}],
        hook_dispatcher=hooks,  # type: ignore[arg-type]
        crash_diagnostics=diagnostics,  # type: ignore[arg-type]
        verification_enabled=False,
        skills_enabled=False,
    )
    return session, (client, surface, hooks, diagnostics, store)


def test_sensitive_policy_redacts_every_durable_and_display_boundary(tmp_path: Path) -> None:
    result = {
        "path": ".env",
        "content": _CANARY,
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    session, captures = _make_session(tmp_path, tool_result=result)
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Read the approved environment file.") == 0
        in_memory_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert log_path is not None
    persisted = log_path.read_text(encoding="utf-8")
    displayed = _serialized([*surface.events, *surface.legacy_events])
    hook_payloads = _serialized(hooks.payloads)
    diagnostic_payloads = _serialized(diagnostics.events)
    replay_messages = _serialized(in_memory_messages)
    consumed_provider_requests = _serialized(client.request_references)
    artifacts = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.rglob("*")
        if path.is_file() and path != log_path
    )

    for boundary_copy in (
        persisted,
        displayed,
        hook_payloads,
        diagnostic_payloads,
        replay_messages,
        consumed_provider_requests,
        artifacts,
    ):
        assert _CANARY not in boundary_copy

    combined = "\n".join(
        (persisted, displayed, hook_payloads, replay_messages, consumed_provider_requests)
    )
    assert "Sensitive tool output redacted." in combined
    assert "environment_file" in combined
    assert ".env" in combined
    assert client.saw_canary_during_call == [False, True]


def test_provider_echo_of_approved_secret_is_taint_redacted_before_every_boundary(
    tmp_path: Path,
) -> None:
    similar_non_secret = "ALYSIS_SECRET_CANARY_7dc6f759"
    echoed_reply = f"Approved value: {_CANARY}. Similar value: {similar_non_secret}."
    result = {
        "path": ".env",
        "content": _CANARY,
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    session, captures = _make_session(
        tmp_path,
        tool_result=result,
        final_response=echoed_reply,
        final_raw={"provider_echo": echoed_reply},
        initial_response="Working safely.",
        stream=True,
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Read the approved environment file.") == 0
        in_memory_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert log_path is not None
    every_boundary = _serialized(
        {
            "session": log_path.read_text(encoding="utf-8"),
            "surface": [*surface.events, *surface.legacy_events],
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
            "messages": in_memory_messages,
            "provider_requests": client.request_references,
        }
    )
    assert _CANARY not in every_boundary
    assert "[redacted: approved sensitive content]" in every_boundary
    assert similar_non_secret in every_boundary
    assert client.stream_flags == [True, False]


def test_exact_taint_redaction_leaves_similar_and_non_sensitive_values_unchanged() -> None:
    arguments = {"path": ".env"}
    result = {
        "path": ".env",
        "content": _CANARY,
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    taints = collect_sensitive_response_taints("fs_read", arguments, result)
    similar = "ALYSIS_SECRET_CANARY_7dc6f759"
    response = LLMResponse(
        content=f"secret={_CANARY}; similar={similar}",
        tool_calls=[],
        raw={"secret": _CANARY, "similar": similar},
    )

    safe = redact_sensitive_response_taints(response, taints)
    ordinary = LLMResponse(content="ordinary", tool_calls=[], raw={"value": "ordinary"})

    assert _CANARY not in _serialized(safe)
    assert similar in safe.content
    assert safe.raw["similar"] == similar
    assert redact_sensitive_response_taints(ordinary, taints) is ordinary


def test_taint_collection_redacts_bare_assignment_and_json_secret_values() -> None:
    result = {
        "path": ".env",
        "content": f'API_TOKEN={_CANARY}\nJSON={{"token":"{_ERROR_CANARY}"}}\n',
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    taints = collect_sensitive_response_taints("fs_read", {"path": ".env"}, result)
    response = LLMResponse(
        content=f"token={_CANARY}; nested={_ERROR_CANARY}",
        tool_calls=[],
        raw={},
    )

    safe = redact_sensitive_response_taints(response, taints)

    assert _CANARY not in safe.content
    assert _ERROR_CANARY not in safe.content


def test_tainted_secret_is_removed_from_follow_up_tool_arguments_before_execution() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="echo-attempt",
                name="shell_run",
                arguments={"command": f"printf {_CANARY}"},
            )
        ],
        raw={"command": f"printf {_CANARY}"},
    )

    safe = redact_sensitive_response_taints(response, {_CANARY})

    assert _CANARY not in _serialized(safe)
    assert safe.tool_calls[0].arguments["command"] == (
        "printf [redacted: approved sensitive content]"
    )


def test_sensitive_exception_text_is_never_replayed_or_persisted(tmp_path: Path) -> None:
    session, captures = _make_session(
        tmp_path,
        tool_error=RuntimeError(f"reader repr exposed {_ERROR_CANARY}"),
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Try the approved environment read.") == 0
        in_memory_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert log_path is not None
    everything = _serialized(
        {
            "session": log_path.read_text(encoding="utf-8"),
            "surface": [*surface.events, *surface.legacy_events],
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
            "messages": in_memory_messages,
            "provider_requests": client.request_references,
        }
    )
    assert _ERROR_CANARY not in everything
    assert all(_ERROR_CANARY not in request for request in client.request_snapshots)
    assert (
        "Sensitive path is protected and will not be readable after this failure. "
        "No content was returned. This failure is terminal; do not retry."
    ) in everything


def test_non_sensitive_values_are_returned_byte_for_byte() -> None:
    arguments = {
        "path": "src/example.py",
        "content": "print('ordinary')\n",
        "edits": [{"expected_old": "ordinary", "new": "updated"}],
    }
    result = {"path": "src/example.py", "content": "ordinary\r\nbytes"}
    boundary = sensitive_tool_boundary("fs_write", arguments, result=result)

    assert boundary.sensitive is False
    assert redact_sensitive_tool_arguments("fs_write", arguments) is arguments
    assert redact_sensitive_tool_result("fs_write", arguments, result) is result


def test_sensitive_provider_response_is_sanitized_before_usage_persistence() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call-sensitive",
                name="fs_write",
                arguments={"path": ".env", "content": _CANARY},
            )
        ],
        raw={"output": [{"arguments": _CANARY}]},
    )

    safe = redact_sensitive_response_for_persistence(response)

    assert safe is not response
    assert safe.raw == {}
    assert safe.tool_calls[0].arguments["content"] == "[redacted: sensitive file content]"
    assert response.tool_calls[0].arguments["content"] == _CANARY


class _ImmutableCall:
    __slots__ = ("_arguments", "_id", "_name", "_provider_metadata")

    def __init__(self) -> None:
        object.__setattr__(self, "_id", "immutable-call")
        object.__setattr__(self, "_name", "fs_write")
        object.__setattr__(self, "_arguments", {"path": ".env", "content": _CANARY})
        object.__setattr__(self, "_provider_metadata", None)

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def arguments(self) -> dict[str, Any]:
        return self._arguments

    @property
    def provider_metadata(self) -> None:
        return self._provider_metadata

    def __copy__(self) -> _ImmutableCall:
        return self


class _ImmutableResponse:
    __slots__ = ("_call",)

    def __init__(self) -> None:
        object.__setattr__(self, "_call", _ImmutableCall())

    @property
    def content(self) -> str:
        return f"echoed {_CANARY}"

    @property
    def tool_calls(self) -> list[_ImmutableCall]:
        return [self._call]

    @property
    def raw(self) -> dict[str, Any]:
        return {"arguments": _CANARY}

    @property
    def response_model(self) -> str:
        return "immutable-model"

    @property
    def usage(self) -> None:
        return None

    @property
    def provider_metadata(self) -> None:
        return None

    @property
    def reasoning(self) -> tuple[Any, ...]:
        return ()

    @property
    def assistant_phase(self) -> None:
        return None

    def __copy__(self) -> _ImmutableResponse:
        return self


def test_immutable_sensitive_response_fallback_is_fail_closed() -> None:
    response = _ImmutableResponse()

    safe = redact_sensitive_response_for_persistence(response)

    assert safe is not response
    assert safe.content == ""
    assert safe.raw == {}
    assert safe.tool_calls[0] is not response.tool_calls[0]
    assert safe.tool_calls[0].arguments["content"] == "[redacted: sensitive file content]"
    assert _CANARY not in _serialized(
        {
            "content": safe.content,
            "raw": safe.raw,
            "arguments": safe.tool_calls[0].arguments,
        }
    )


def test_sensitive_error_result_is_never_sent_or_persisted(tmp_path: Path) -> None:
    # A tool that RETURNS an error payload under a sensitive policy (rather
    # than raising) must get the same fail-closed redaction on every boundary.
    result = {
        "error": f"lookup repr exposed {_ERROR_CANARY}",
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["credential_file"],
        },
    }
    session, captures = _make_session(
        tmp_path,
        tool_result=result,
        final_response="Used the approved result.",
        initial_response="Preparing the lookup.",
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Use the approved lookup.") == 0
    finally:
        session.close()

    assert all(_ERROR_CANARY not in request for request in client.request_snapshots)
    assert log_path is not None
    durable_and_display = _serialized(
        {
            "session": log_path.read_text(encoding="utf-8"),
            "surface": [*surface.events, *surface.legacy_events],
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
            "consumed_requests": client.request_references,
        }
    )
    assert _ERROR_CANARY not in durable_and_display
    assert (
        "Sensitive path is protected and will not be readable after this failure. "
        "No content was returned. This failure is terminal; do not retry."
    ) in durable_and_display
