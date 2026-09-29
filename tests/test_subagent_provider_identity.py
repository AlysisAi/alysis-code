from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

import httpx
import pytest

from alysis_code import agent_loop
from alysis_code.agent_loop import ToolDef, build_tools
from alysis_code.config import AppConfig
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionStore
from alysis_code.subagents import SubagentDefinition
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.usage_tracker import UsageSummary


@pytest.fixture(autouse=True)
def _forbid_http_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_request(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Scheduler identity tests must not make HTTP requests.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_request)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "isolated-config"))


class _ChildSession:
    def __init__(self, *, root: Path, store: SessionStore) -> None:
        self.root = root
        self.store = store
        self.messages: list[dict[str, Any]] = []
        self.initial_messages: list[dict[str, Any]] = []
        self.usage_summary = UsageSummary()
        self.workspace_touched_paths: set[str] = set()
        self.tools = {
            "fs_read": ToolDef(
                name="fs_read",
                description="Read a file.",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            )
        }
        self.tool_list = [tool.as_openai_tool() for tool in self.tools.values()]

    def run_turn(self, task: str, *, cancellation_token: Any = None) -> int:
        self.initial_messages = list(self.messages)
        final = f"Analysis completed: {task}"
        self.messages.extend(
            [{"role": "user", "content": task}, {"role": "assistant", "content": final}]
        )
        self.store.append("user_message", {"content": task})
        self.store.append("final", {"content": final})
        return 0

    def close(self) -> None:
        self.store.close()


class _ChildFactory:
    def __init__(self, *, sessions_dir: Path, identity_kind: str) -> None:
        self.sessions_dir = sessions_dir
        self.identity_kind = identity_kind
        self.calls: list[dict[str, Any]] = []
        self.children: list[_ChildSession] = []
        self.lock = Lock()

    def __call__(self, **kwargs: Any) -> _ChildSession:
        child = _ChildSession(
            root=kwargs["root"],
            store=SessionStore(
                enabled=True,
                sessions_dir=self.sessions_dir,
                session_id=uuid4().hex,
                cwd=str(kwargs["root"]),
                repo_root=None,
                owner="tests",
            ),
        )
        if self.identity_kind != "legacy":
            initial_identity = (
                uuid4().hex if self.identity_kind == "explicit" else child.store.session_id
            )
            child.provider_session_id = kwargs.get("provider_session_id") or initial_identity
        with self.lock:
            self.calls.append(kwargs)
            self.children.append(child)
        return child


@contextmanager
def _scheduler_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, identity_kind: str
) -> Iterator[tuple[dict[str, ToolDef], _ChildFactory]]:
    root = tmp_path / "repo"
    root.mkdir()
    sessions_dir = tmp_path / "sessions"
    parent_store = SessionStore(
        enabled=True,
        sessions_dir=sessions_dir,
        session_id="parent-log",
        cwd=str(root),
        repo_root=None,
        owner="tests",
    )
    factory = _ChildFactory(sessions_dir=sessions_dir, identity_kind=identity_kind)
    monkeypatch.setattr(agent_loop, "create_session", factory)
    tools = build_tools(
        root=root,
        console=None,
        store=parent_store,
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        api_key="test-key",
        non_interactive=True,
        surface=NoopSurface(),
        subagents_enabled=True,
        subagent_registry={
            "explorer": SubagentDefinition(
                name="explorer",
                description="Inspect repository code.",
                system_prompt="Inspect the requested code.",
                mode="readonly",
                allow_tools=("fs_read",),
                allow_workspace_writes=False,
            )
        },
        create_session_factory=factory,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        yield tools, factory
    finally:
        scheduler = tools["subagent_run"].run.__self__.child_scheduler
        if scheduler is not None:
            scheduler.shutdown(cancel_pending=True)
        for child in factory.children:
            child.close()
        parent_store.close()


def _wait(tools: dict[str, ToolDef], launched: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in launched, launched
    run_id = launched["run_id"]
    waited = tools["subagent_wait"].run({"run_id": run_id})
    result = waited["results"][run_id]
    assert result.get("status") == "success", result
    return result


@pytest.mark.parametrize("launch_tool", ["subagent_run", "subagent_spawn"])
@pytest.mark.parametrize("identity_kind", ["explicit", "default", "legacy"])
def test_repeated_child_resumes_preserve_provider_identity_with_fresh_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_tool: str, identity_kind: str
) -> None:
    with _scheduler_tools(tmp_path, monkeypatch, identity_kind=identity_kind) as (tools, factory):
        first = tools[launch_tool].run({"name": "explorer", "task": "Inspect initial state."})
        first_result = first if launch_tool == "subagent_run" else _wait(tools, first)
        assert first_result.get("status") == "success", first_result
        original = factory.children[0]
        original_provider_id = getattr(original, "provider_session_id", original.store.session_id)

        resumed = tools["subagent_resume"].run(
            {"run_id": first["run_id"], "task": "Inspect the next part."}
        )
        resumed_result = _wait(tools, resumed)
        resumed_again = tools["subagent_resume"].run(
            {"run_id": resumed["run_id"], "task": "Finish the analysis."}
        )
        last_result = _wait(tools, resumed_again)

        assert "provider_session_id" not in factory.calls[0]
        assert [call["provider_session_id"] for call in factory.calls[1:]] == [
            original_provider_id,
            original_provider_id,
        ]
        log_ids = [child.store.session_id for child in factory.children]
        assert len(set(log_ids)) == 3
        assert "parent-log" not in log_ids
        assert original_provider_id not in log_ids[1:]
        assert [
            result["subagent_session_id"] for result in (first_result, resumed_result, last_result)
        ] == log_ids
        assert len({first["run_id"], resumed["run_id"], resumed_again["run_id"]}) == 3
        assert resumed_result["resumed_from"] == first["run_id"]
        assert last_result["resumed_from"] == resumed["run_id"]
        assert all(child.store.path.is_file() for child in factory.children)
        restored_contents = [
            message.get("content") for message in factory.children[-1].initial_messages
        ]
        assert "Inspect initial state." in restored_contents
        assert "Inspect the next part." in restored_contents
        assert restored_contents.count("Inspect initial state.") == 1
        assert restored_contents.count("Inspect the next part.") == 1


@pytest.mark.parametrize("identity_kind", ["default", "legacy"])
def test_sibling_resumes_keep_separate_provider_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_kind: str
) -> None:
    with _scheduler_tools(tmp_path, monkeypatch, identity_kind=identity_kind) as (tools, factory):
        alpha = tools["subagent_spawn"].run({"name": "explorer", "task": "Inspect alpha."})
        beta = tools["subagent_spawn"].run({"name": "explorer", "task": "Inspect beta."})
        alpha_result = _wait(tools, alpha)
        beta_result = _wait(tools, beta)
        alpha_id = alpha_result["subagent_session_id"]
        beta_id = beta_result["subagent_session_id"]
        assert alpha_id != beta_id
        assert all("provider_session_id" not in call for call in factory.calls)

        for source, task, expected_identity in (
            (beta, "Continue beta.", beta_id),
            (alpha, "Continue alpha.", alpha_id),
        ):
            resumed = tools["subagent_resume"].run({"run_id": source["run_id"], "task": task})
            result = _wait(tools, resumed)
            assert factory.calls[-1]["provider_session_id"] == expected_identity
            assert result["subagent_session_id"] not in {alpha_id, beta_id, "parent-log"}

        assert len({child.store.session_id for child in factory.children}) == 4
        assert {call["prompt_cache_parent_session_id"] for call in factory.calls} == {"parent-log"}
