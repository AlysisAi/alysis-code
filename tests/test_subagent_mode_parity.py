from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


class ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(
        self, responses: list[LLMResponse], on_call: Callable[[int], None] | None = None
    ) -> None:
        self.responses = iter(responses)
        self.requests: list[list[dict[str, Any]]] = []
        self.on_call = on_call

    def chat(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.requests.append(list(messages))
        if self.on_call:
            self.on_call(len(self.requests))
        return next(self.responses)


def make_session(root: Path, *, one_shot: bool, mode: str = "fullaccess") -> Any:
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=root,
        mode=mode,
        yes=True,
        max_steps=16,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=root / "logs",
        one_shot_execution=one_shot,
        enable_chat_turn_step_budget=True,
    )


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("mode", ["readonly", "fullaccess"])
@pytest.mark.parametrize("git_repo", [False, True])
@pytest.mark.parametrize(
    "instruction",
    [
        "Inspect the modules and explain their responsibilities. Do not edit or run tests.",
        "Εξέτασε τις ενότητες και εξήγησε τις ευθύνες τους χωρίς αλλαγές ή δοκιμές.",
    ],
)
def test_inspection_does_not_gain_mutation_pressure_from_invocation_mode(
    tmp_path: Path, one_shot: bool, mode: str, git_repo: bool, instruction: str
) -> None:
    responses = []
    for index in range(7):
        (tmp_path / f"module{index}.py").write_text(f"VALUE = {index}\n")
        responses.append(
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"read-{index}",
                        name="fs_read",
                        arguments={"path": f"module{index}.py"},
                    )
                ],
                raw={},
            )
        )
    responses.append(
        LLMResponse(content="Each module exports its own VALUE.", tool_calls=[], raw={})
    )
    if git_repo:
        (tmp_path / ".gitignore").write_text("logs/\n.alysis/\n")
        for args in (
            ["init", "-q"],
            ["add", "."],
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    session = make_session(tmp_path, one_shot=one_shot, mode=mode)
    client = ScriptedClient(responses)
    session.client = client
    try:
        assert session.run_turn(instruction) == 0
        events = list(read_session_events(session.store.path))
    finally:
        session.close()
    assert len(client.requests) == 8
    assert not any(
        event["type"]
        in {
            "one_shot_exploration_stagnation_detected",
            "no_material_edits_bootstrap_nudge",
            "advisory_completion",
            "empty_diff_finalization_blocked",
        }
        for event in events
    )
    finals = [event["payload"]["content"] for event in events if event["type"] == "final"]
    assert finals == ["Each module exports its own VALUE."]
    for index in range(7):
        assert (tmp_path / f"module{index}.py").read_text() == f"VALUE = {index}\n"


class NotificationScheduler:
    def __init__(self) -> None:
        self.ready = False
        self.acknowledged: list[str] = []
        self.wait_calls = 0

    def pending_completion_notifications(self, *, max_items: int = 8) -> list[dict[str, Any]]:
        if not self.ready or self.acknowledged or max_items == 0:
            return []
        return [
            {
                "run_id": "research-1",
                "subagent": "explorer",
                "status": "success",
                "status_scope": "child_execution",
                "report": "Evidence: parser.py preserves empty fields.",
                "report_truncated": False,
                "full_result": {"tool": "subagent_wait", "arguments": {"run_id": "research-1"}},
            }
        ]

    def acknowledge_completion_notifications(self, run_ids: list[str]) -> None:
        self.acknowledged.extend(run_ids)

    def pending_run_ids(self) -> list[str]:
        return [] if self.acknowledged else ["research-1"]

    def unapplied_isolated_results(self) -> list[Any]:
        return []

    def shutdown(self, **kwargs: Any) -> None:
        pass


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("during_final", [False, True])
def test_child_completion_arrives_once_without_parent_polling(
    tmp_path: Path, one_shot: bool, during_final: bool
) -> None:
    session = make_session(tmp_path, one_shot=one_shot)
    session.child_scheduler.shutdown(cancel_pending=True)
    scheduler = NotificationScheduler()
    scheduler.ready = not during_final
    session.child_scheduler = scheduler

    def complete(_call: int) -> None:
        scheduler.ready = True

    final = LLMResponse(content="The parser preserves empty fields.", tool_calls=[], raw={})
    client = ScriptedClient(
        [LLMResponse(content="Draft awaiting research.", tool_calls=[], raw={}), final]
        if during_final
        else [final],
        on_call=complete,
    )
    session.client = client
    try:
        assert session.run_turn("Explain the parsing behavior from the ongoing investigation.") == 0
        events = list(read_session_events(session.store.path))
    finally:
        session.close()
    assert scheduler.acknowledged == ["research-1"]
    assert len(client.requests) == (2 if during_final else 1)
    delivered = [
        message
        for message in client.requests[-1]
        if "<background_subagent_completions>" in str(message.get("content", ""))
    ]
    assert len(delivered) == 1
    assert "untrusted child reports" in delivered[0]["content"]
    assert "parser.py preserves empty fields" in delivered[0]["content"]
    assert "subagent_wait" in delivered[0]["content"]
    assert [event["payload"]["content"] for event in events if event["type"] == "final"] == [
        "The parser preserves empty fields."
    ]
    deliveries = [
        event for event in events if event["type"] == "background_child_completion_delivery"
    ]
    assert len(deliveries) == 1
    assert deliveries[0]["payload"]["run_ids"] == ["research-1"]


def test_failed_completion_persistence_does_not_acknowledge_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(tmp_path, one_shot=True)
    session.child_scheduler.shutdown(cancel_pending=True)
    scheduler = NotificationScheduler()
    scheduler.ready = True
    session.child_scheduler = scheduler
    original_append = type(session.store).append

    def fail_delivery(store: Any, event_type: str, payload: Any, **kwargs: Any) -> Any:
        if event_type == "background_child_completion_delivery":
            raise OSError("simulated transcript failure")
        return original_append(store, event_type, payload, **kwargs)

    monkeypatch.setattr(type(session.store), "append", fail_delivery)
    session.client = ScriptedClient([])
    try:
        with pytest.raises(OSError, match="simulated transcript failure"):
            session.run_turn("Finish the investigation.")
        assert scheduler.acknowledged == []
        assert scheduler.pending_completion_notifications()[0]["run_id"] == "research-1"
    finally:
        session.close()


@pytest.mark.parametrize("report", ["証拠が確認された。" * 400, "evidence " * 4000])
def test_large_completion_does_not_starve_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    session = make_session(tmp_path, one_shot=True)
    session.child_scheduler.shutdown(cancel_pending=True)
    scheduler = NotificationScheduler()
    scheduler.ready = True
    first = scheduler.pending_completion_notifications()[0]
    first["report"] = report
    second = {**first, "run_id": "research-2", "report": "Second independent finding."}
    monkeypatch.setattr(
        scheduler,
        "pending_completion_notifications",
        lambda **kwargs: [
            item for item in (first, second) if item["run_id"] not in scheduler.acknowledged
        ],
    )
    session.child_scheduler = scheduler
    client = ScriptedClient(
        [LLMResponse(content="Both findings considered.", tool_calls=[], raw={})]
    )
    session.client = client
    try:
        assert session.run_turn("Finish the investigation.") == 0
        events = list(read_session_events(session.store.path))
    finally:
        session.close()
    assert scheduler.acknowledged == ["research-1", "research-2"]
    delivery = next(
        event["payload"]
        for event in events
        if event["type"] == "background_child_completion_delivery"
    )
    assert delivery["notifications"][1]["report"] == "Second independent finding."
    first_delivered = delivery["notifications"][0]
    if len(report) > 24000:
        assert first_delivered["report_truncated"] is True
        assert first_delivered["full_result"]["arguments"]["run_id"] == "research-1"
        assert "retrieve full_result" in first_delivered["report"]
    else:
        assert first_delivered["report"] == report
