"""First-dispatch contract for the pinned task brief.

Kept free of any post-fix API so it can be run unchanged against the base
commit: there it fails because the model's first request carried
``status: awaiting_substantive_repo_request`` next to the real instruction.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.prompt_context import _TASK_BRIEF_EMPTY_STATUS, _TASK_BRIEF_MARKER
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall


class _SnapshotClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], **_: Any) -> LLMResponse:  # type: ignore[no-untyped-def]
        # Captured at call time: later in-place edits of the session cannot
        # make this look correct after the fact.
        self.requests.append(copy.deepcopy(messages))
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def _brief_in(messages: list[dict[str, Any]]) -> str:
    briefs = [
        str(m.get("content") or "")
        for m in messages
        if str(m.get("role") or "") == "user"
        and isinstance(m.get("content"), str)
        and str(m.get("content")).startswith(_TASK_BRIEF_MARKER)
    ]
    assert len(briefs) == 1, briefs
    return briefs[0]


@pytest.mark.parametrize(
    ("mode", "instruction", "responses"),
    [
        (
            "auto",
            "Implement a bounded job scheduler and test cancellation.",
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="w1",
                            name="fs_write",
                            arguments={"path": "scheduler.py", "content": "pass\n"},
                        )
                    ],
                    raw={},
                ),
                LLMResponse(content="Done.", tool_calls=[], raw={}),
            ],
        ),
        (
            "readonly",
            "Review the error handling in the config loader and explain the risks.",
            [LLMResponse(content="Review complete.", tool_calls=[], raw={})],
        ),
    ],
)
def test_first_model_request_names_the_accepted_task_not_an_awaiting_placeholder(
    tmp_path: Path, mode: str, instruction: str, responses: list[LLMResponse]
) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", skills_enabled=False),
        root=tmp_path,
        mode=mode,
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="override-key",
        verification_enabled=False,
    )
    client = _SnapshotClient(responses)
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn(instruction) == 0
    finally:
        session.close()

    first_brief = _brief_in(client.requests[0])
    assert _TASK_BRIEF_EMPTY_STATUS not in first_brief, (
        "the first model request still carried the empty placeholder beside a real task"
    )
    assert instruction in first_brief
