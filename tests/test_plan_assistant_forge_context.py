from __future__ import annotations

from typing import Any

from alysis_code import plan_assistant
from alysis_code.config import AppConfig, ConfigError
from alysis_code.model_router import ROLE_PLANNER


def _run_planner_until_model_resolution(*, prefer_context: str | None, monkeypatch: Any) -> str:
    captured: dict[str, Any] = {}

    def fake_resolve_model_for_role(**kwargs: Any) -> str:
        captured.update(kwargs)
        raise ConfigError("stop after model resolution")

    monkeypatch.setattr(plan_assistant, "resolve_model_for_role", fake_resolve_model_for_role)
    kwargs: dict[str, Any] = {}
    if prefer_context is not None:
        kwargs["prefer_context"] = prefer_context

    plan_assistant.run_planner_turn(
        cfg=AppConfig(
            model="default-model",
            extra_fields={
                "role_models": {"planner": "subagent-planner"},
                "forge_role_models": {"planner": "forge-planner"},
            },
        ),
        api_key_override="sk-test",
        plan={},
        transcript_tail=[],
        user_text="update the plan",
        **kwargs,
    )

    assert captured["role"] == ROLE_PLANNER
    return str(captured.get("prefer_context"))


def test_run_planner_turn_with_forge_context_uses_forge_role_models(
    monkeypatch: Any,
) -> None:
    assert (
        _run_planner_until_model_resolution(
            prefer_context="forge",
            monkeypatch=monkeypatch,
        )
        == "forge"
    )


def test_run_planner_turn_default_context_uses_subagent_role_models(
    monkeypatch: Any,
) -> None:
    assert (
        _run_planner_until_model_resolution(
            prefer_context=None,
            monkeypatch=monkeypatch,
        )
        == "default"
    )
