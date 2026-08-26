from __future__ import annotations

from alysis_code.plan_assistant import PLANNER_SYSTEM_PROMPT
from alysis_code.plan_mode import PLAN_MODE_SYSTEM_PROMPT


def _assert_language_script_policy(prompt: str) -> None:
    assert (
        "Choose the natural response language" in prompt
        or "choose the natural reply language" in prompt
        or "language/script describe the reply language" in prompt
    )
    assert "Latin" in prompt
    assert "explicit" in prompt
    assert "malformed" in prompt or "ambiguous" in prompt or "gibberish" in prompt
    assert (
        "Never translate code identifiers, file paths, CLI commands, config keys, or code blocks"
        in prompt
    )


def test_plan_mode_prompt_has_language_script_policy() -> None:
    _assert_language_script_policy(PLAN_MODE_SYSTEM_PROMPT)


def test_plan_mode_prompt_has_workspace_grounding_policy() -> None:
    assert "Treat host-provided workspace context as the source of truth" in PLAN_MODE_SYSTEM_PROMPT
    assert "describe the area generically instead of inventing details" in PLAN_MODE_SYSTEM_PROMPT


def test_planner_prompt_has_language_script_policy() -> None:
    _assert_language_script_policy(PLANNER_SYSTEM_PROMPT)
