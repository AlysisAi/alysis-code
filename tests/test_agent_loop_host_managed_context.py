from __future__ import annotations

from types import SimpleNamespace

import pytest

from alysis_code import agent_loop as agent_loop_mod
from alysis_code.agent.turn_contract import TurnEffect, TurnOutcome, TurnSemantics


@pytest.mark.parametrize(
    "text",
    [
        "<skill_context>\nsource: discovered skill bundles\n</skill_context>\n",
        "<matched_skill_context>\nsource: host lexical matcher\n</matched_skill_context>\n",
        "<explicit_skill_context>\nsource: user-selected skill for this turn only\n</explicit_skill_context>\n",
        "<repo_conventions>\nsource: repo-authored conventions files\n</repo_conventions>\n",
        "Repository conventions context\n- legacy marker\n",
        "<workspace_binding_context>\nrepo root: .\n</workspace_binding_context>\n",
    ],
)
def test_is_host_managed_user_context_message_recognizes_wrappers_and_legacy_markers(
    text: str,
) -> None:
    assert agent_loop_mod._is_host_managed_user_context_message(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "<skill_context>\nsource: discovered skill bundles\n</skill_context>\n",
        "<matched_skill_context>\nsource: host lexical matcher\n</matched_skill_context>\n",
        "<explicit_skill_context>\nsource: user-selected skill for this turn only\n</explicit_skill_context>\n",
        "<repo_conventions>\nsource: repo-authored conventions files\n</repo_conventions>\n",
    ],
)
def test_task_brief_candidate_ignores_host_managed_skill_and_convention_wrappers(
    text: str,
) -> None:
    # Host-managed context wrappers are never task candidates, so neither the
    # legacy router-contract entry point nor the host state accepts them.
    assert agent_loop_mod.instruction_is_task_candidate(text) is False
    session = SimpleNamespace(
        messages=[],
        store=SimpleNamespace(workspace_kind="git_repo", session_id="wrapper-session"),
        subagent_depth=0,
    )
    refreshed = agent_loop_mod.refresh_session_task_brief_message(
        session,
        pending_instruction=text,
        turn_semantics=TurnSemantics(
            outcome=TurnOutcome.CHANGE,
            requested_effects=(TurnEffect.WRITE_WORKSPACE,),
        ),
        route="repo",
    )
    # Only the placeholder was inserted; no task was installed from the wrapper.
    assert refreshed is True
    assert getattr(session, "task_state", None) is None
    assert agent_loop_mod._empty_task_brief_message() == session.messages[0]["content"]


def test_recent_visible_non_repo_history_excludes_host_managed_skill_and_convention_wrappers() -> (
    None
):
    messages = [
        {"role": "user", "content": "Thanks."},
        {"role": "assistant", "content": "You're welcome."},
        {
            "role": "user",
            "content": "<skill_context>\nsource: discovered skill bundles\n</skill_context>\n",
        },
        {"role": "assistant", "content": "I can inspect the repo."},
        {
            "role": "user",
            "content": (
                "<matched_skill_context>\nsource: host lexical matcher\n</matched_skill_context>\n"
            ),
        },
        {
            "role": "user",
            "content": (
                "<explicit_skill_context>\nsource: user-selected skill for this turn only\n"
                "</explicit_skill_context>\n"
            ),
        },
        {"role": "assistant", "content": "Which file should I focus on?"},
        {
            "role": "user",
            "content": "<repo_conventions>\nsource: repo-authored conventions files\n</repo_conventions>\n",
        },
        {"role": "user", "content": "Fix retry handling in parser.py."},
    ]

    history = agent_loop_mod._recent_visible_non_repo_history(messages)
    assert history == [
        {"role": "user", "content": "Thanks."},
        {"role": "assistant", "content": "You're welcome."},
        {"role": "assistant", "content": "I can inspect the repo."},
        {"role": "assistant", "content": "Which file should I focus on?"},
    ]
    assert not any("<skill_context>" in row["content"] for row in history)
    assert not any("<repo_conventions>" in row["content"] for row in history)
