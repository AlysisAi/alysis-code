"""Vendored-path edit advisory: warn once when an edit lands in a vendored tree.

Observed in a scored run: the agent rewrote files under ``sklearn/externals``
(vendored joblib), breaking dozens of unrelated tests. The advisory names the
paths at the first such edit and never blocks; it fires at most once per turn.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta: Any | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, tool_choice, stream, on_text_delta, temperature
        if self.calls >= len(self._responses):
            raise AssertionError("scripted response exhausted")
        response = self._responses[self.calls]
        self.calls += 1
        return response


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir(exist_ok=True)
    for args in (
        ["init"],
        ["config", "user.name", "Test User"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(
            ["git", "-C", os.fspath(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", os.fspath(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _events_of(sessions_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return list(read_session_events(sessions_dir / f"{session_id}.jsonl"))


def _payloads(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in events if event.get("type") == event_type]


def test_vendored_edit_advisory_fires_once_and_names_the_paths(tmp_path: Path) -> None:
    _init_git_repo_with_commit(tmp_path)
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override="vendored-advisory",
    )
    session.client = _ScriptedClient(  # type: ignore[assignment]
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={
                            "path": "pkg/externals/dep/mod.py",
                            "content": "VALUE = 1\n",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="fs_write",
                        arguments={
                            "path": "pkg/externals/dep/other.py",
                            "content": "OTHER = 2\n",
                        },
                    )
                ],
                raw={},
            ),
            *[LLMResponse(content="Done.", tool_calls=[], raw={}) for _ in range(8)],
        ]
    )

    try:
        exit_code = session.run_turn("Set VALUE in pkg/externals/dep/mod.py.")
    finally:
        session.close()

    events = _events_of(sessions_dir, "vendored-advisory")
    advisories = _payloads(events, "vendored_path_edit_advisory")

    assert exit_code == 0
    assert len(advisories) == 1, "advisory must fire exactly once per turn"
    assert advisories[0]["vendored_paths"] == ["pkg/externals/dep/mod.py"]
    assert "vendored" in advisories[0]["message"]


def test_first_party_edits_do_not_trigger_the_advisory(tmp_path: Path) -> None:
    _init_git_repo_with_commit(tmp_path)
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override="vendored-advisory-none",
    )
    session.client = _ScriptedClient(  # type: ignore[assignment]
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "pkg/core/mod.py", "content": "VALUE = 1\n"},
                    )
                ],
                raw={},
            ),
            *[LLMResponse(content="Done.", tool_calls=[], raw={}) for _ in range(8)],
        ]
    )

    try:
        exit_code = session.run_turn("Set VALUE in pkg/core/mod.py.")
    finally:
        session.close()

    events = _events_of(sessions_dir, "vendored-advisory-none")

    assert exit_code == 0
    assert not _payloads(events, "vendored_path_edit_advisory")
