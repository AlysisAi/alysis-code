from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_subagent_lifecycle_delivery import _finished, _reader_registry
from test_subagent_mode_parity import ScriptedClient, make_session
from test_subagents import _build_main_tools

from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


@pytest.mark.parametrize("cleanup", ["collect", "cancel"])
def test_coordinator_cleanup_preserves_report_until_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry=_reader_registry()
    )
    launcher = tools["subagent_run"].run.__self__
    scheduler = launcher.child_scheduler
    report = "parser.py:17 preserves empty fields."
    monkeypatch.setattr(
        launcher,
        "run_registered",
        lambda *args, **kwargs: {"status": "success", "result": report, "subagent": "explorer"},
    )
    try:
        spawned = tools["subagent_spawn"].run({"name": "explorer", "task": "Inspect source."})
        run_id = spawned["run_id"]
        _finished(scheduler, run_id)
        if cleanup == "collect":
            result = scheduler.collect(run_id=run_id, consume_delivery=False)
            assert result["results"][run_id]["result"] == report
        else:
            result = tools["subagent_cancel"].run({"run_id": run_id})
            assert result["already_finished_run_ids"] == [run_id]
        pending = scheduler.pending_completion_notifications()
        assert [item["run_id"] for item in pending] == [run_id]
        assert pending[0]["report"] == report

        # Explicit waits still consume the report they actually return.
        result = tools["subagent_wait"].run({"run_id": run_id})
        assert result["results"][run_id]["result"] == report
        assert scheduler.pending_completion_notifications() == []
    finally:
        scheduler.shutdown(cancel_pending=True)


class _ExitScheduler:
    """Finish children only when the parent reaches its host cleanup boundary."""

    def __init__(self, *, count: int = 1) -> None:
        self.ready = False
        self.cancelled = False
        self.acknowledged: list[str] = []
        self.collect_consumption: list[bool] = []
        self.reports = [
            {
                "run_id": f"child-{index}",
                "subagent": "explorer",
                "status": "success",
                "status_scope": "child_execution",
                "report": f"Evidence {index}: " + "x" * 3900,
                "full_result": {
                    "tool": "subagent_wait",
                    "arguments": {"run_id": f"child-{index}"},
                },
            }
            for index in range(count)
        ]

    def pending_run_ids(self) -> list[str]:
        if self.cancelled:
            return []
        return [item["run_id"] for item in self.reports if item["run_id"] not in self.acknowledged]

    def pending_completion_notifications(self, *, max_items: int = 8) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        return [item for item in self.reports if item["run_id"] not in self.acknowledged][
            :max_items
        ]

    def acknowledge_completion_notifications(self, run_ids: list[str]) -> None:
        assert not set(run_ids).intersection(self.acknowledged)
        self.acknowledged.extend(run_ids)

    def collect(
        self, *, run_id: Any, consume_delivery: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        self.collect_consumption.append(consume_delivery)
        self.ready = True
        if consume_delivery:
            self.acknowledge_completion_notifications(list(run_id))
        return {"results": {item["run_id"]: item for item in self.reports}}

    def cancel(self, *, run_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.ready = True
        self.cancelled = True
        for report in self.reports:
            report["status"] = "cancelled"
        return {"cancelled_run_ids": list(run_id)}

    def unapplied_isolated_results(self) -> list[Any]:
        return []

    def shutdown(self, **kwargs: Any) -> None:
        pass


def _budget_session(tmp_path: Path, *, one_shot: bool, policy: str, count: int = 1) -> Any:
    session = make_session(tmp_path, one_shot=one_shot)
    session.child_scheduler.shutdown(cancel_pending=True)
    session.child_scheduler = _ExitScheduler(count=count)
    session.cfg.subagent_orchestration.turn_end_policy = policy
    session.chat_turn_fixed_override = 1
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    session.client = ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="read", name="fs_read", arguments={"path": "module.py"})],
                raw={},
            ),
            LLMResponse(content="Stopped at the configured step budget.", tool_calls=[], raw={}),
        ]
    )
    return session


def _deliveries(session: Any) -> list[dict[str, Any]]:
    return [
        event["payload"]
        for event in read_session_events(session.store.path)
        if event["type"] == "background_child_completion_delivery"
    ]


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("policy", ["wait", "cancel"])
def test_budget_exit_delivers_all_cleanup_reports_once_and_restores_history(
    tmp_path: Path, one_shot: bool, policy: str
) -> None:
    session = _budget_session(tmp_path, one_shot=one_shot, policy=policy, count=10)
    scheduler = session.child_scheduler
    try:
        assert session.run_turn("Inspect the module within this turn's budget.") == (
            1 if one_shot else 0
        )
        assert len(session.client.requests) == 2  # No extra model call after budget exhaustion.
        assert scheduler.collect_consumption == ([False] if policy == "wait" else [])
        deliveries = _deliveries(session)
        assert len(deliveries) > 1
        assert all(
            len(json.dumps(item["notifications"], ensure_ascii=False, sort_keys=True)) <= 24_000
            for item in deliveries
        )
        assert scheduler.acknowledged == [f"child-{index}" for index in range(10)]
        assert scheduler.pending_completion_notifications() == []
        restored = _load_chat_resume_messages(session.store.path)
        historical = [
            message for message in restored if "Historical subagent reports" in message["content"]
        ]
        assert len(historical) == len(deliveries)
        assert all(message["role"] == "assistant" for message in historical)
        assert "full_result" not in json.dumps(historical)
        for index in range(10):
            assert json.dumps(historical).count(f"Evidence {index}: ") == 1

        # A later parent turn has the retained evidence, without another delivery.
        session.client = ScriptedClient(
            [LLMResponse(content="Reviewed the evidence.", tool_calls=[], raw={})]
        )
        assert session.run_turn("Review the retained findings.") == 0
        assert len(_deliveries(session)) == len(deliveries)
        assert any(
            "<background_subagent_completions>" in str(message.get("content"))
            for message in session.client.requests[0]
        )
    finally:
        session.close()


@pytest.mark.parametrize(
    "failure_event", ["background_child_completion_delivery", "controller_intervention"]
)
def test_failed_exit_delivery_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_event: str
) -> None:
    session = _budget_session(tmp_path, one_shot=False, policy="wait")
    scheduler = session.child_scheduler
    original_append = type(session.store).append

    def fail_append(store: Any, event_type: str, payload: Any, **kwargs: Any) -> Any:
        if event_type == failure_event and (
            event_type == "background_child_completion_delivery"
            or payload.get("detail") == "background_child_completion_delivery"
        ):
            raise OSError("simulated exit delivery failure")
        return original_append(store, event_type, payload, **kwargs)

    monkeypatch.setattr(type(session.store), "append", fail_append)
    try:
        with pytest.raises(OSError, match="simulated exit delivery failure"):
            session.run_turn("Inspect the module within this turn's budget.")
        assert scheduler.collect_consumption == [False]
        assert scheduler.acknowledged == []
        assert scheduler.pending_completion_notifications()[0]["run_id"] == "child-0"

        monkeypatch.setattr(type(session.store), "append", original_append)
        session.client = ScriptedClient(
            [LLMResponse(content="Reviewed the evidence.", tool_calls=[], raw={})]
        )
        assert session.run_turn("Continue with the retained findings.") == 0
        assert scheduler.acknowledged == ["child-0"]
        assert scheduler.pending_completion_notifications() == []
        # A later intervention-write failure can repeat a durable envelope;
        # delivery remains at least once without silently losing evidence.
        assert len(_deliveries(session)) == (2 if failure_event == "controller_intervention" else 1)
    finally:
        session.close()


def test_parent_cancellation_persists_completed_cleanup_reports(tmp_path: Path) -> None:
    session = _budget_session(tmp_path, one_shot=False, policy="cancel")
    session.chat_turn_fixed_override = 2
    token = InteractiveCancellationToken()
    session.client.on_call = lambda _call: token.cancel()
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn("Inspect the module.", cancellation_token=token)
        assert session.child_scheduler.acknowledged == ["child-0"]
        assert _deliveries(session)[0]["notifications"][0]["status"] == "cancelled"
        assert "Evidence 0:" in json.dumps(_load_chat_resume_messages(session.store.path))
    finally:
        session.close()
