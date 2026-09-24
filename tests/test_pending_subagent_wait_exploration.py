from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from alysis_code.agent.turn.exploration import (
    _is_action_progress_tool,
    _is_exploration_only_tool,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


@pytest.mark.parametrize("interrupted", [False, True])
def test_pending_wait_is_neither_exploration_nor_material_progress(interrupted: bool) -> None:
    result = {
        "results": {},
        "pending_run_ids": ["child-a"],
        "wait_pending": True,
        "wait_interrupted": interrupted,
    }
    assert not _is_action_progress_tool("subagent_wait", result=result)
    assert not _is_exploration_only_tool("subagent_wait", result=result)


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("also_read", [False, True])
def test_waiting_on_real_child_does_not_hide_or_manufacture_exploration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    one_shot: bool,
    also_read: bool,
) -> None:
    """Exercise the native scheduler, tools and turn guard across pending waits.

    A held child makes every timed wait really expire. Optional repository reads
    in the same batches must still trigger the ordinary exploration guard.
    """
    for index in range(6):
        (tmp_path / f"context-{index}.txt").write_text(f"context {index}\n")
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=15,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "sessions",
        one_shot_execution=one_shot,
        enable_chat_turn_step_budget=not one_shot,
        verification_enabled=False,
    )
    started = Event()
    release = Event()
    scheduler = session.child_scheduler
    assert scheduler is not None

    def held_child(_args: dict[str, Any], *, run_id: str, **_kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=20)
        return {
            "run_id": run_id,
            "subagent": "general",
            "status": "success",
            "result": "The scoped review is complete.",
            "effects": ["delegate", "read_workspace"],
            "touched_repo_paths": [],
        }

    monkeypatch.setattr(scheduler.launcher, "run_registered", held_child)
    spawned = session.tools["subagent_spawn"].run(
        {"name": "general", "task": "Review the context files.", "mode": "readonly"}
    )
    assert "run_id" in spawned, spawned
    assert started.wait(timeout=2)

    class Client:
        model = "test-model"
        temperature = 0.2
        calls = 0

        def chat(self, **_kwargs: Any) -> LLMResponse:
            step = self.calls
            self.calls += 1
            if step == 0:
                # Real parent mutation authorizes the execution guard, as in
                # the live run that exposed the false wait warning.
                calls = [
                    ToolCall(
                        id="write",
                        name="fs_write",
                        arguments={"path": "out.txt", "content": "done\n"},
                    )
                ]
            elif step <= 6:
                calls = [
                    ToolCall(
                        id=f"wait-{step}",
                        name="subagent_wait",
                        arguments={"run_id": spawned["run_id"], "timeout_s": 0.001},
                    )
                ]
                if also_read:
                    calls.append(
                        ToolCall(
                            id=f"read-{step}",
                            name="fs_read",
                            arguments={"path": f"context-{step - 1}.txt"},
                        )
                    )
            elif step == 7:
                release.set()
                calls = [
                    ToolCall(
                        id="join",
                        name="subagent_wait",
                        arguments={"run_id": spawned["run_id"], "timeout_s": 2},
                    )
                ]
            else:
                assert step < 10, "unexpected extra parent calls"
                return LLMResponse(
                    content="Wrote out.txt and collected the review.", tool_calls=[], raw={}
                )
            return LLMResponse(content="", tool_calls=calls, raw={})

    session.client = Client()  # type: ignore[assignment]
    try:
        assert session.run_turn("Write out.txt with done, then collect the review.") == 0
    finally:
        release.set()
        session.close()
    events = list(read_session_events(session.store.path))
    nudges = [event for event in events if event.get("type") == "exploration_nudge"]
    assert bool(nudges) == also_read
    progress = [event for event in events if event.get("type") == "subagent_orchestration_progress"]
    assert all(event["payload"]["step"] >= 8 for event in progress)
    pending_results = []
    for message in session.messages:
        if message.get("role") != "tool" or not str(message.get("tool_call_id", "")).startswith(
            "wait-"
        ):
            continue
        pending_results.append(json.loads(message["content"]))
    assert len(pending_results) == 6
    assert all(
        result["wait_pending"] is True and result["results"] == {} for result in pending_results
    )
    assert (tmp_path / "out.txt").read_text() == "done\n"
