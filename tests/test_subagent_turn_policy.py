from __future__ import annotations

import time
from threading import Event
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent.steering import SteerInbox
from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.types import AssistantResponsePhase
from alysis_code.session_store import read_session_events
from alysis_code.subagents import built_in_subagents


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        on_reasoning_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse:
        _ = on_text_delta, on_reasoning_delta, temperature, cancellation_token
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "stream": stream,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected extra model call")
        return self._responses.pop(0)


class _UnexpectedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        raise AssertionError("model should not be called")


class _FakeBackgroundScheduler:
    def __init__(
        self,
        run_ids: list[str],
        *,
        unapplied_run_ids: tuple[str, ...] = (),
    ) -> None:
        self.pending = list(run_ids)
        self.unapplied_run_ids = tuple(unapplied_run_ids)
        self.collect_calls: list[list[str]] = []
        self.cancel_calls: list[list[str]] = []
        self.shutdown_calls = 0

    def pending_run_ids(self) -> list[str]:
        return list(self.pending)

    def unapplied_isolated_results(self) -> list[dict[str, str]]:
        return [{"run_id": run_id} for run_id in self.unapplied_run_ids]

    def collect(
        self,
        *,
        run_id: str | list[str] | None = "all",
        timeout_s: float | None = None,
        cancellation_token: Any | None = None,
        consume_delivery: bool = True,
    ) -> dict[str, Any]:
        _ = timeout_s, cancellation_token, consume_delivery
        selected = (
            list(self.pending)
            if run_id is None or run_id == "all"
            else ([run_id] if isinstance(run_id, str) else list(run_id))
        )
        self.collect_calls.append(selected)
        self.pending = [candidate for candidate in self.pending if candidate not in selected]
        return {
            "results": {
                candidate: {
                    "run_id": candidate,
                    "subagent": "explorer",
                    "result": f"report from {candidate}",
                    "usage": {},
                }
                for candidate in selected
            },
            "pending_run_ids": [],
            "wait_pending": False,
        }

    def cancel(
        self,
        *,
        run_id: str | list[str] | None = "all",
        wait_for_running: bool = True,
        wait_timeout_s: float | None = None,
    ) -> dict[str, Any]:
        _ = (wait_for_running, wait_timeout_s)
        selected = (
            list(self.pending)
            if run_id is None or run_id == "all"
            else ([run_id] if isinstance(run_id, str) else list(run_id))
        )
        self.cancel_calls.append(selected)
        self.pending = [candidate for candidate in self.pending if candidate not in selected]
        return {"cancelled_run_ids": selected, "children": []}

    def shutdown(self, *, cancel_pending: bool = True) -> None:
        self.shutdown_calls += 1
        if cancel_pending:
            self.pending.clear()


def _replace_child_scheduler(
    session: Any,
    scheduler: _FakeBackgroundScheduler,
) -> None:
    original = session.child_scheduler
    if original is not None:
        original.shutdown(cancel_pending=True)
    session.child_scheduler = scheduler


def _event_payloads(path, event_type: str) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _replace_subagent_run_with_fake(session: Any) -> list[dict[str, Any]]:
    original = session.tools["subagent_run"]
    calls: list[dict[str, Any]] = []

    def _fake_subagent_run(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(args))
        return {
            "subagent": str(args.get("name") or "explorer"),
            "subagent_session_id": "fake-subagent",
            "result": "subagent report",
            "usage": {},
            "sandbox": {"mode": "readonly", "tools": ["fs_read"]},
        }

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original.description,
        parameters=original.parameters,
        run=_fake_subagent_run,
        metadata=original.metadata,
    )
    return calls


def _replace_background_subagent_tools_with_fake(session: Any) -> list[str]:
    calls: list[str] = []

    def _replace(name: str, run) -> None:  # type: ignore[no-untyped-def]
        original = session.tools[name]
        session.tools[name] = ToolDef(
            name=name,
            description=original.description,
            parameters=original.parameters,
            run=run,
            metadata=original.metadata,
        )

    def _spawn(args: dict[str, Any]) -> dict[str, Any]:
        calls.append("subagent_spawn")
        run_id = str(args.get("label") or "background-child")
        return {"run_id": run_id, "state": "spawned", "queued": False}

    def _status(_args: dict[str, Any]) -> dict[str, Any]:
        calls.append("subagent_status")
        return {
            "children": [
                {
                    "run_id": run_id,
                    "subagent": "explorer",
                    "workspace_view": "shared",
                    "state": "running",
                    "steps_completed": 1,
                    "elapsed_ms": 10,
                }
                for run_id in ("child-a", "child-b")
            ],
            "summary": {"running": 2},
            "unapplied_isolated_results": [],
        }

    def _wait(_args: dict[str, Any]) -> dict[str, Any]:
        calls.append("subagent_wait")
        return {
            "results": {
                run_id: {
                    "run_id": run_id,
                    "subagent": "explorer",
                    "status": "success",
                    "result": f"report from {run_id}",
                    "steps_completed": 1,
                    "elapsed_ms": 20,
                    "effects": ["delegate", "read_workspace"],
                    "touched_repo_paths": [],
                }
                for run_id in ("child-a", "child-b")
            },
            "pending_run_ids": [],
            "pending_children": [],
            "wait_pending": False,
            "summary": {"joined": 2},
            "message": "All selected background children are joined.",
        }

    _replace("subagent_spawn", _spawn)
    _replace("subagent_status", _status)
    _replace("subagent_wait", _wait)
    return calls


def test_subagent_turn_policy_never_manufactures_delegation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Router-free path: no semantic contract exists, so a turn never derives a
    # required_by_user delegation gate (nor a user_opt_out block) from the
    # instruction text — in any language. Execution intent must not manufacture
    # a recommendation either: subagents remain an optional capability.
    registry = built_in_subagents()
    tools = {"subagent_run": object()}  # type: ignore[dict-item]

    for instruction in (
        "Please use a subagent to inspect the parser before answering.",
        "Run the explorer to map the parser.",
        "استخدم وكيلاً فرعياً لفحص المستودع.",
        "Fix the parser, but do not use subagents for this one.",
        "サブエージェントを使わずに、このリポジトリを確認してください。",
    ):
        advisory = agent_loop._resolve_subagent_turn_policy(
            instruction=instruction,
            subagents_enabled=True,
            subagent_depth=0,
            subagent_registry=registry,
            turn_tools=tools,  # type: ignore[arg-type]
            repo_turn_execution_intent="advisory_non_execution",
        )
        assert advisory.level == "available", instruction
        assert advisory.reason == "subagents_available", instruction

        execute = agent_loop._resolve_subagent_turn_policy(
            instruction=instruction,
            subagents_enabled=True,
            subagent_depth=0,
            subagent_registry=registry,
            turn_tools=tools,  # type: ignore[arg-type]
            repo_turn_execution_intent="execute",
        )
        assert execute.level == "available", instruction
        assert execute.reason == "subagents_available", instruction

    context = agent_loop._subagent_turn_context_message(
        agent_loop._resolve_subagent_turn_policy(
            instruction="Inspect this repo and fix any issue you find.",
            subagents_enabled=True,
            subagent_depth=0,
            subagent_registry=registry,
            turn_tools=tools,  # type: ignore[arg-type]
            repo_turn_execution_intent="execute",
        )
    )
    assert context is not None
    assert "policy: available" in context
    assert "optional capability metadata" in context
    assert "Make an explicit delegation decision" not in context
    assert "verifier" in context
    assert "test-strategist" not in context
    assert "test-strategy" not in context
    assert "Call subagent_run before finalizing" not in context
    assert "Use subagent_spawn" not in context
    assert "unapplied_isolated_run_ids" not in context


def test_subagent_turn_policy_reports_disabled_and_missing_tool_as_off(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = built_in_subagents()
    tools = {"subagent_run": object()}  # type: ignore[dict-item]

    disabled = agent_loop._resolve_subagent_turn_policy(
        instruction="Please use a subagent to inspect the repo.",
        subagents_enabled=False,
        subagent_depth=0,
        subagent_registry=registry,
        turn_tools=tools,  # type: ignore[arg-type]
        repo_turn_execution_intent="advisory_non_execution",
    )
    assert disabled.level == "off"
    assert disabled.reason == "subagents_disabled"
    assert agent_loop._subagent_turn_context_message(disabled) is None

    tool_missing = agent_loop._resolve_subagent_turn_policy(
        instruction="Please use a subagent to inspect the repo.",
        subagents_enabled=True,
        subagent_depth=0,
        subagent_registry=registry,
        turn_tools={},
        repo_turn_execution_intent="advisory_non_execution",
    )
    assert tool_missing.level == "off"
    assert tool_missing.reason == "subagent_tool_not_exposed"


@pytest.mark.parametrize(
    "instruction",
    [
        "Please use a subagent to read README.md and tell me what it says. Do not modify files.",
        "Read README.md and tell me what it says. Do not modify files.",
    ],
    ids=["explicit-delegation", "model-chosen-delegation"],
)
def test_repo_turn_injects_delegation_decision_context_and_delegation_executes(
    tmp_path, instruction
) -> None:  # type: ignore[no-untyped-def]
    # Router-free path: no semantic contract forces or forbids delegation. The
    # turn gets neutral capability metadata, and a model-initiated subagent_run
    # simply runs.
    (tmp_path / "README.md").write_text("repo notes\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    stale_run_id = "already-applied-run"
    _replace_child_scheduler(
        session,
        _FakeBackgroundScheduler([], unapplied_run_ids=(stale_run_id,)),
    )
    subagent_calls = _replace_subagent_run_with_fake(session)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Delegating now.",
                tool_calls=[
                    ToolCall(
                        id="call-subagent",
                        name="subagent_run",
                        arguments={
                            "name": "explorer",
                            "task": "Inspect README.md and report the relevant notes.",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Done after using the subagent.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn(instruction)
        session_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(subagent_calls) == 1
    assert subagent_calls[0]["name"] == "explorer"
    assert len(client.calls) == 2
    first_request = client.calls[0]["messages"]
    first_call_messages = "\n".join(str(message.get("content") or "") for message in first_request)
    assert "<subagent_turn_context>" in first_call_messages
    assert "policy: available" in first_call_messages
    assert "optional capability metadata" in first_call_messages
    assert "Make an explicit delegation decision" not in first_call_messages
    assert stale_run_id not in first_call_messages
    context_messages = [
        message
        for message in first_request
        if "<subagent_turn_context>" in str(message.get("content") or "")
    ]
    assert len(context_messages) == 1
    assert context_messages[0]["role"] == "system"
    assert all(
        "<subagent_turn_context>" not in str(message.get("content") or "")
        for message in first_request
        if message.get("role") == "user"
    )
    # No manufactured delegation gate exists on the router-free path.
    assert "Call subagent_run before finalizing" not in first_call_messages
    assert _event_payloads(session_path, "subagent_required_nudge") == []


def test_readonly_background_orchestration_finalizes_without_mutation_pressure(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    background_tool_calls = _replace_background_subagent_tools_with_fake(session)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Starting both read-only investigations.",
                tool_calls=[
                    ToolCall(
                        id=f"spawn-{run_id}",
                        name="subagent_spawn",
                        arguments={
                            "name": "explorer",
                            "label": run_id,
                            "task": f"Inspect the source assigned to {run_id}.",
                            "mode": "readonly",
                            "workspace_view": "shared",
                        },
                    )
                    for run_id in ("child-a", "child-b")
                ],
                raw={},
            ),
            LLMResponse(
                content="Checking both children once.",
                tool_calls=[ToolCall(id="status-all", name="subagent_status", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="Collecting both reports.",
                tool_calls=[ToolCall(id="wait-all", name="subagent_wait", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="Both read-only investigations completed successfully.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Coordinate two read-only investigations and report.") == 0
        session_path = session.store.path
    finally:
        session.close()

    assert len(client.calls) == 4
    assert sorted(background_tool_calls[:2]) == ["subagent_spawn", "subagent_spawn"]
    assert background_tool_calls[2:] == ["subagent_status", "subagent_wait"]
    injected_context = "\n".join(
        str(message.get("content") or "")
        for call in client.calls
        for message in call["messages"]
        if message.get("role") == "system"
    )
    assert "Use the next step to start implementation" not in injected_context
    assert "Continue execution now" not in injected_context
    assert _event_payloads(session_path, "completion_gate_nudge") == []
    assert _event_payloads(session_path, "continuation_nudge") == []
    assert _event_payloads(session_path, "implementation_bootstrap_nudge") == []
    progress = _event_payloads(session_path, "subagent_orchestration_progress")
    assert [event["tool"] for event in progress] == [
        "subagent_spawn",
        "subagent_spawn",
        "subagent_status",
        "subagent_wait",
    ]


def test_background_launch_guard_stops_sequential_replay_even_if_parent_state_changes(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    original = session.tools["subagent_spawn"]
    dispatched: list[dict[str, Any]] = []

    def _spawn(arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(dict(arguments))
        return {
            "run_id": str(arguments["run_id"]),
            "state": "spawned",
            "queued": False,
        }

    session.tools["subagent_spawn"] = ToolDef(
        name="subagent_spawn",
        description=original.description,
        parameters=original.parameters,
        run=_spawn,
        metadata=original.metadata,
    )
    original_apply = session.tools["subagent_apply"]
    apply_calls: list[dict[str, Any]] = []

    def _apply(arguments: dict[str, Any]) -> dict[str, Any]:
        apply_calls.append(dict(arguments))
        return {
            "ok": True,
            "applied_paths": ["candidate.txt"],
            "semantic_no_progress": False,
        }

    session.tools["subagent_apply"] = ToolDef(
        name="subagent_apply",
        description=original_apply.description,
        parameters=original_apply.parameters,
        run=_apply,
        metadata=original_apply.metadata,
    )
    objective = {
        "name": "explore",
        "task": "Inspect the same typed objective.  ",
        "workspace_view": "shared",
    }
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Launching two deliberate replicas together.",
                tool_calls=[
                    ToolCall(
                        id="spawn-original",
                        name="subagent_spawn",
                        arguments={**objective, "run_id": "child-a"},
                    ),
                    ToolCall(
                        id="spawn-replica",
                        name="subagent_spawn",
                        arguments={**objective, "run_id": "child-b"},
                    ),
                ],
                raw={},
            ),
            LLMResponse(
                content="Relabelling the same objective and changing unrelated parent state.",
                tool_calls=[
                    ToolCall(
                        id="spawn-rotated",
                        name="subagent_spawn",
                        arguments={
                            **objective,
                            "name": "explorer",
                            "task": "Inspect the same typed objective.",
                            "mode": "readonly",
                            "run_id": "phase-five-child-a",
                        },
                    ),
                    ToolCall(
                        id="apply-unrelated",
                        name="subagent_apply",
                        arguments={"run_id": "some-other-candidate"},
                    ),
                ],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Coordinate the background investigation.") == 1
        session_path = session.store.path
    finally:
        session.close()

    assert len(client.calls) == 2
    assert [call["run_id"] for call in dispatched] == ["child-a", "child-b"]
    assert apply_calls == [{"run_id": "some-other-candidate"}]
    coalesced = _event_payloads(session_path, "root_subagent_launch_coalesced")
    assert [event["requested_run_id"] for event in coalesced] == ["phase-five-child-a"]
    assert [event["stop_signal"] for event in coalesced] == [True]
    backstop = _event_payloads(session_path, "root_subagent_semantic_repetition_backstop")
    assert len(backstop) == 1
    assert backstop[0]["source"] == "background_launch_intent"
    assert backstop[0]["reason"] == "launch_intent_repeated_after_generation"


def test_readonly_background_launch_backstop_uses_neutral_fallback(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    original = session.tools["subagent_spawn"]
    dispatched: list[dict[str, Any]] = []

    def _spawn(arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(dict(arguments))
        return {
            "run_id": str(arguments["run_id"]),
            "state": "spawned",
            "queued": False,
        }

    session.tools["subagent_spawn"] = ToolDef(
        name="subagent_spawn",
        description=original.description,
        parameters=original.parameters,
        run=_spawn,
        metadata=original.metadata,
    )
    objective = {
        "name": "explorer",
        "task": "Inspect the same typed objective.",
        "mode": "readonly",
        "workspace_view": "shared",
    }
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Launching two deliberate replicas together.",
                tool_calls=[
                    ToolCall(
                        id="spawn-readonly-a",
                        name="subagent_spawn",
                        arguments={**objective, "run_id": "readonly-a"},
                    ),
                    ToolCall(
                        id="spawn-readonly-b",
                        name="subagent_spawn",
                        arguments={**objective, "run_id": "readonly-b"},
                    ),
                ],
                raw={},
            ),
            LLMResponse(
                content="Replaying the completed read-only objective.",
                tool_calls=[
                    ToolCall(
                        id="spawn-readonly-c",
                        name="subagent_spawn",
                        arguments={**objective, "run_id": "readonly-c"},
                    )
                ],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Coordinate the read-only background investigation.") == 1
        session_path = session.store.path
    finally:
        session.close()

    assert [call["run_id"] for call in dispatched] == ["readonly-a", "readonly-b"]
    final_text = str(_event_payloads(session_path, "final")[-1]["content"])
    assert "Implementation has not started" not in final_text
    assert "Finish the requested implementation" not in final_text
    assert "No verification result was recorded" not in final_text
    assert "Complete the requested result or report a concrete blocker" in final_text


def test_turn_proceeds_without_subagent_gate_when_subagents_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Router-free path: subagents disabled means the policy is silently off —
    # no unavailable-notice message, no gate events, and the turn completes on
    # the first model reply.
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", subagents_enabled=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
        subagents_enabled=False,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="I inspected directly because subagent delegation is unavailable.",
                tool_calls=[],
                raw={},
            )
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Please use a subagent to inspect the repo.")
        session_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 1
    first_call_messages = "\n".join(
        str(message.get("content") or "") for message in client.calls[0]["messages"]
    )
    assert "<subagent_turn_context>" not in first_call_messages
    assert _event_payloads(session_path, "subagent_request_unavailable") == []
    assert _event_payloads(session_path, "subagent_required_nudge") == []


def test_interactive_repo_exploration_gets_no_host_delegation_nudge(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "README.md").write_text("known issue\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Listing files.",
                tool_calls=[ToolCall(id="call-list", name="fs_list", arguments={"path": "."})],
                raw={},
            ),
            LLMResponse(
                content="Reading notes.",
                tool_calls=[
                    ToolCall(
                        id="call-read",
                        name="fs_read",
                        arguments={"path": "README.md"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Blocked by no concrete issue found.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Inspect this repo and fix any issue you find.")
        session_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 3
    third_call_messages = "\n".join(
        str(message.get("content") or "") for message in client.calls[2]["messages"]
    )
    assert "Subagent delegation check" not in third_call_messages
    events = _event_payloads(session_path, "subagent_exploration_nudge")
    assert events == []


def test_explicit_subagent_request_nudge_text_remains_available() -> None:
    policy = agent_loop._SubagentTurnPolicy(
        level="required_by_user",
        reason="explicit_user_request",
        available_subagents=("explorer",),
    )

    message = agent_loop._subagent_required_nudge_message(policy)

    assert "explicitly asked for subagent or delegation behavior" in message
    assert "Use the next tool-enabled step to call subagent_run" in message


def test_model_initiated_subagent_call_runs_despite_opt_out_phrasing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Router-free path: no semantic contract exists, so opt-out phrasing in
    # the instruction cannot manufacture a tool block — honoring "do not use
    # subagents" is the model's job, and a subagent_run it does issue simply
    # executes.
    (tmp_path / "README.md").write_text("repo notes\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    subagent_calls = _replace_subagent_run_with_fake(session)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Trying a subagent.",
                tool_calls=[
                    ToolCall(
                        id="call-subagent",
                        name="subagent_run",
                        arguments={
                            "name": "explorer",
                            "task": "Inspect README.md and report the notes.",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="README.md contains repo notes.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Tell me what README.md says, but do not use subagents.")
        session_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(subagent_calls) == 1
    tool_results = _event_payloads(session_path, "tool_result")
    assert tool_results
    assert tool_results[0]["name"] == "subagent_run"
    assert "error" not in (tool_results[0].get("result") or {})


def test_turn_end_nudges_then_waits_and_injects_background_results(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sessions_dir = tmp_path / "sessions"
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    cfg.subagent_orchestration.turn_end_policy = "wait"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    scheduler = _FakeBackgroundScheduler(["run-a", "run-b"])
    _replace_child_scheduler(session, scheduler)
    client = _ScriptedClient(
        [
            LLMResponse(content="First premature answer.", tool_calls=[], raw={}),
            LLMResponse(content="Second premature answer.", tool_calls=[], raw={}),
            LLMResponse(content="Third premature answer.", tool_calls=[], raw={}),
            LLMResponse(content="Final answer using both reports.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Give me a short answer after background research.")
        session_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 4
    assert scheduler.collect_calls == [["run-a", "run-b"]]
    assert scheduler.cancel_calls == []
    enforcement = _event_payloads(session_path, "subagent_turn_end_enforcement")
    assert [event["action"] for event in enforcement] == ["nudge", "nudge", "wait"]
    assert all(event["policy"] == "wait" for event in enforcement)
    assert all(event["run_ids"] == ["run-a", "run-b"] for event in enforcement)
    fourth_request = "\n".join(
        str(message.get("content") or "") for message in client.calls[3]["messages"]
    )
    assert "<background_subagent_results>" in fourth_request
    assert "report from run-a" in fourth_request
    assert "report from run-b" in fourth_request


def test_identical_background_wait_interruption_notice_is_rendered_once(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    sessions_dir = tmp_path / "sessions"
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    cfg.subagent_orchestration.turn_end_policy = "wait"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=7,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    started = Event()
    release = Event()
    scheduler = session.child_scheduler
    assert scheduler is not None

    def _blocking_child(
        _args: dict[str, Any],
        *,
        run_id: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=3.0)
        return {
            "run_id": run_id,
            "subagent": "explorer",
            "result": "child report",
            "usage": {},
            "effects": ["delegate", "read_workspace"],
            "touched_repo_paths": [],
        }

    monkeypatch.setattr(scheduler.launcher, "run_registered", _blocking_child)
    spawned = session.tools["subagent_spawn"].run(
        {"name": "explorer", "task": "Wait for the release signal."}
    )
    assert "run_id" in spawned, spawned
    assert started.wait(timeout=2.0)
    inbox = session.steer_inbox
    assert isinstance(inbox, SteerInbox)

    class _InterruptingClient(_ScriptedClient):
        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            call_number = len(self.calls) + 1
            if call_number in {3, 4}:
                inbox.signal_waiters(
                    reason="child_inactive",
                    run_id=spawned["run_id"],
                )
            if call_number == 5:
                release.set()
            return super().chat(**kwargs)

    client = _InterruptingClient(
        [
            LLMResponse(content=f"Premature answer {index}.", tool_calls=[], raw={})
            for index in range(1, 6)
        ]
        + [LLMResponse(content="Final answer with child report.", tool_calls=[], raw={})]
    )
    session.client = client  # type: ignore[assignment]
    notice = (
        "The background-child wait was interrupted while the children remain "
        "running. Handle the pending parent wake reason (child_inactive) before "
        "waiting again."
    )

    try:
        assert session.run_turn("Return a report after background research.") == 0
        assert (
            sum(
                message.get("role") == "system" and message.get("content") == notice
                for message in session.messages
            )
            == 1
        )
    finally:
        release.set()
        session.close()


def test_turn_end_cancel_policy_cancels_children_and_marks_final_answer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sessions_dir = tmp_path / "sessions"
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    cfg.subagent_orchestration.turn_end_policy = "cancel"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=5,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    scheduler = _FakeBackgroundScheduler(["run-c"])
    _replace_child_scheduler(session, scheduler)
    client = _ScriptedClient(
        [
            LLMResponse(content="First premature answer.", tool_calls=[], raw={}),
            LLMResponse(content="Second premature answer.", tool_calls=[], raw={}),
            LLMResponse(content="Final answer without the child.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Give me a short answer after background research.")
        session_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 3
    assert scheduler.collect_calls == []
    assert scheduler.cancel_calls == [["run-c"]]
    enforcement = _event_payloads(session_path, "subagent_turn_end_enforcement")
    assert [event["action"] for event in enforcement] == ["nudge", "nudge", "cancel"]
    assert all(event["policy"] == "cancel" for event in enforcement)
    final_payloads = _event_payloads(session_path, "final")
    assert "Background subagents were cancelled" in final_payloads[-1]["content"]


def test_turn_end_ignores_cancelled_uncollected_child(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    scheduler = session.child_scheduler
    assert scheduler is not None
    child_started = Event()

    def _cancelled_child(
        _args: dict[str, Any],
        *,
        run_id: str,
        cancellation_token: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        child_started.set()
        while not cancellation_token.is_cancelled:
            time.sleep(0.005)
        return {
            "run_id": run_id,
            "subagent": "explorer",
            "status": "cancelled",
            "error": "Subagent cancelled by the parent turn.",
            "usage": {},
            "effects": ["delegate", "read_workspace"],
            "touched_repo_paths": [],
        }

    monkeypatch.setattr(scheduler.launcher, "run_registered", _cancelled_child)
    spawned = session.tools["subagent_spawn"].run(
        {"name": "explorer", "task": "Inspect cancellation state."}
    )
    assert child_started.wait(timeout=2.0)
    scheduler.cancel(run_id=spawned["run_id"], wait_for_running=False)
    deadline = time.monotonic() + 2.0
    while not scheduler._children[spawned["run_id"]].completion.done():
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert scheduler._children[spawned["run_id"]].collected is False

    client = _ScriptedClient(
        [
            LLMResponse(
                content="Cancellation is complete; here is the report.", tool_calls=[], raw={}
            )
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Report the cancelled investigation state.")
        session_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 1
    assert _event_payloads(session_path, "subagent_turn_end_enforcement") == []


def test_report_only_subagent_status_does_not_acquire_material_edit_expectation(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    sessions_dir = tmp_path / "sessions"
    session_id = "report-only-subagent-status"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
        enable_chat_turn_step_budget=True,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="status-1",
                        name="subagent_status",
                        arguments={"run_id": "all"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="No child runs are active.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn(
            "Inspect subagent state and report only; do not change anything."
        )
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 2
    event_types = [
        event["type"] for event in read_session_events(sessions_dir / f"{session_id}.jsonl")
    ]
    assert "interactive_completion_gate_failed" not in event_types


def test_retained_worktree_notice_is_appended_only_to_terminal_final(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        enable_chat_turn_step_budget=True,
    )
    run_id = "retained-implementation"
    scheduler = _FakeBackgroundScheduler([], unapplied_run_ids=(run_id,))
    _replace_child_scheduler(session, scheduler)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Implementation report before the final decision point.",
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
            LLMResponse(
                content="Complete implementation report after the decision point.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Finish the implementation and report it.") == 0
        session_path = session.store.path
    finally:
        session.close()

    assistant_payloads = _event_payloads(session_path, "assistant_message")
    assert assistant_payloads[0]["content"] == (
        "Implementation report before the final decision point."
    )
    final_content = _event_payloads(session_path, "final")[-1]["content"]
    assert "Complete implementation report after the decision point." in final_content
    assert (
        f"Retained isolated subagent results await subagent_apply or subagent_discard: {run_id}."
    ) in final_content
