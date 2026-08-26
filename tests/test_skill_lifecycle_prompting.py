from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code.agent.turn_contract import (
    TurnEffect,
    TurnOutcome,
    TurnSemantics,
    TurnTarget,
    TurnTargetKind,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse


class _CaptureClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = on_text_delta, temperature
        self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
        return LLMResponse(content="Done.", tool_calls=[], raw={})


def test_skill_lifecycle_uses_provider_neutral_capability_semantics() -> None:
    semantics = TurnSemantics(
        outcome=TurnOutcome.MANAGE_CAPABILITY,
        requested_effects=(TurnEffect.WRITE_WORKSPACE,),
        targets=(
            TurnTarget(
                kind=TurnTargetKind.CAPABILITY,
                value="pytest debugging skill",
                evidence_quote="pytest debugging skill",
            ),
        ),
    )

    assert semantics.execution_posture == "execute"
    assert semantics.targets[0].kind is TurnTargetKind.CAPABILITY


def test_run_turn_uses_canonical_skill_lifecycle_guidance_for_first_skill_request_in_empty_workspace(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            skills_enabled=True,
            routing_mode="code_only",
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    client = _CaptureClient()
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn(
            "Create the first reusable skill for pytest debugging in this workspace."
        )
    finally:
        session.close()

    assert exit_code == 0
    tool_names = {
        str(item.get("function", {}).get("name") or "")
        for item in client.calls[0]["tools"] or []
        if isinstance(item, dict)
    }
    assert "skill_read" not in tool_names
    system_messages = [
        str(message.get("content") or "")
        for message in client.calls[0]["messages"]
        if str(message.get("role") or "") == "system"
    ]
    joined = "\n".join(system_messages)
    assert "Skills lifecycle" in joined
    assert "Skills lifecycle turn" not in joined
    assert "alysis skill init" in joined
    assert "alysis skill create" in joined
    assert "alysis skill validate" in joined
    assert "Do not hand-build skill bundles with `fs_mkdir` or `fs_write`" in joined
    assert "Use `skill_read` only for existing skills and only if available" in joined
    assert "Avoid broad docs/tests spelunking before lifecycle commands" in joined


def test_run_turn_does_not_inject_removed_skill_lifecycle_turn_nudge_for_unrelated_repo_change_request(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            skills_enabled=True,
            routing_mode="code_only",
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )
    client = _CaptureClient()
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Implement search command and update tests.")
    finally:
        session.close()

    assert exit_code == 0
    assert not any(
        "Skills lifecycle turn" in str(message.get("content") or "")
        or "This turn is about skills lifecycle/authoring." in str(message.get("content") or "")
        for message in client.calls[0]["messages"]
    )
