"""A failed child startup must not consume its retained read evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)
from test_subagents import _build_main_tools

from alysis_code import agent_loop
from alysis_code.agent import session as session_mod
from alysis_code.agent import subagent_execution
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.subagents import SubagentDefinition


@pytest.mark.parametrize("changed", [False, True])
def test_failed_start_preserves_read_state_but_revalidates_file_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    target = tmp_path / "source.txt"
    target.write_text("original source\n")
    children: list[session_mod.AgentSession] = []

    class Client:
        model = "offline-delivery-model"
        temperature = 0.0

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, **_kwargs: Any) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="read", name="fs_read", arguments={"path": "source.txt"})
                    ],
                    raw={},
                )
            return LLMResponse(content="Source inspected.", tool_calls=[], raw={})

    def create_child(**kwargs: Any) -> session_mod.AgentSession:
        kwargs.update(
            api_key_override="unused-offline-key",
            session_log_dir_override=tmp_path / "sessions",
            enable_compaction=False,
            verification_enabled=False,
        )
        child = session_mod.create_session(**kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(session_mod, "_make_session_llm_client", lambda **_kwargs: Client())
    monkeypatch.setattr(agent_loop, "create_session", create_child)
    tools = _build_main_tools(
        tmp_path=tmp_path,
        cfg=AppConfig(model=Client.model, stream=False, skills_enabled=False),
        skills_enabled=False,
        subagents_enabled=True,
        non_interactive=True,
        subagent_registry={
            "reader": SubagentDefinition(
                name="reader",
                description="Read source.",
                system_prompt="Read source.",
                mode="readonly",
                allow_tools=("fs_read",),
                allow_workspace_writes=False,
            )
        },
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler

    def resume(run_id: str, task: str):
        launched = tools["subagent_resume"].run({"run_id": run_id, "task": task})
        assert "error" not in launched
        child = scheduler._children[launched["run_id"]]
        child.completion.result(timeout=10)
        child.worker_bookkeeping_completion.result(timeout=10)
        return child

    try:
        first = tools["subagent_run"].run({"name": "reader", "task": "Inspect source."})
        assert first["status"] == "success"
        prior_snapshot = children[0].read_ledger.snapshot()
        assert "source.txt" in prior_snapshot
        with monkeypatch.context() as patch:

            def fail_start(**_kwargs: Any):
                raise RuntimeError("Simulated session creation failure")

            patch.setattr(subagent_execution, "_create_session_for_subagent", fail_start)
            failed = resume(first["run_id"], "Continue source inspection.")
        assert failed.sub_session is None
        assert failed.resume_context.read_ledger_snapshot == prior_snapshot
        if changed:
            target.write_text("changed source\n")
        recovered = resume(failed.run_id, "Inspect source after recovery.")
        assert recovered.resume_context.read_ledger_snapshot == prior_snapshot
        assert (
            recovered.resume_context.read_ledger_snapshot
            is not failed.resume_context.read_ledger_snapshot
        )
        result = next(
            json.loads(message["content"])
            for message in reversed(children[-1].messages)
            if message.get("role") == "tool"
        )
        if changed:
            assert "changed source" in result["content"]
            assert not result.get("read_ledger_skipped")
        else:
            assert result["read_ledger_skipped"] is True
        # A live ledger reset is authoritative; do not resurrect older coverage.
        children[-1].read_ledger.reset()
        after_reset = resume(recovered.run_id, "Inspect after history reset.")
        assert after_reset.resume_context.read_ledger_snapshot == {}
    finally:
        scheduler.shutdown(cancel_pending=True)
        for child in children:
            child.close()
