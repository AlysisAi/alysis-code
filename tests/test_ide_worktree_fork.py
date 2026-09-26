from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.session_store import SessionStore


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def request(bridge, output, method, params, request_id):
    bridge.process_line(
        json.dumps({"protocol_version": "1", "id": request_id, "method": method, "params": params})
    )
    return next(
        json.loads(line)
        for line in output.getvalue().splitlines()
        if json.loads(line).get("id") == request_id
    )


@pytest.fixture
def worktree_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-fixture-key")
    monkeypatch.setenv("ALYSIS_API_KEY", "test-fixture-key")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    target = tmp_path / "worktree"
    git(root, "worktree", "add", "-b", "alysis/test", str(target))

    def create(**kwargs):
        session_id = kwargs["session_id_override"]
        workspace = kwargs["root"]
        store = SessionStore(
            enabled=True,
            sessions_dir=tmp_path / "logs",
            session_id=session_id,
            cwd=str(workspace),
            repo_root=str(workspace),
        )
        return SimpleNamespace(
            messages=[{"role": "system", "content": "Workspace instructions"}],
            store=store,
            close=store.close,
        )

    output = io.StringIO()
    bridge = stdio_bridge.StdioBridge(
        stdout=output,
        create_session_fn=create,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    for name, workspace in [("source", root), ("target", target)]:
        result = request(
            bridge,
            output,
            "session.create",
            {
                "session_id": name,
                "workspace": str(workspace),
                "mode": "readonly",
                "workspace_trusted": True,
            },
            f"create-{name}",
        )
        assert result["ok"], result
    yield bridge, output, root, target, tmp_path
    bridge.close()


def test_fork_crosses_protocol_git_and_persistent_conversation_boundary(worktree_bridge):
    bridge, output, root, target, tmp_path = worktree_bridge
    source = bridge._sessions["source"]
    destination = bridge._sessions["target"]
    source.agent_session.messages.extend(
        [
            {"role": "user", "content": "Remember the feature name: Aurora."},
            {"role": "assistant", "content": "The feature is Aurora."},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "old-call"}]},
            {"role": "tool", "content": "old workspace result", "tool_call_id": "old-call"},
        ]
    )
    result = request(
        bridge,
        output,
        "session.fork",
        {"session_id": "target", "source_session_id": "source"},
        "fork",
    )
    assert result["ok"], result
    assert result["result"]["history_count"] == 2
    assert destination.root == target
    assert source.root == root
    assert not source.closed
    assert destination.change_ledger is None
    assert destination.workspace_fence != source.workspace_fence
    assert all(message["role"] != "tool" for message in destination.agent_session.messages)
    from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages

    persisted = _load_chat_resume_messages(tmp_path / "logs" / "target.jsonl")
    assert persisted == destination.agent_session.messages[1:]
    assert any(
        event.get("type") == "message_end"
        for event in map(json.loads, output.getvalue().splitlines())
    )
    again = request(
        bridge,
        output,
        "session.fork",
        {"session_id": "target", "source_session_id": "source"},
        "again",
    )
    assert not again["ok"]


def test_fork_rejects_untrusted_or_unrelated_workspaces(worktree_bridge):
    bridge, output, root, target, tmp_path = worktree_bridge
    bridge._sessions["target"].workspace_trusted = False
    result = request(
        bridge,
        output,
        "session.fork",
        {"session_id": "target", "source_session_id": "source"},
        "untrusted",
    )
    assert not result["ok"]
    bridge._sessions["target"].workspace_trusted = True
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git(unrelated, "init")
    bridge._sessions["target"].root = unrelated
    result = request(
        bridge,
        output,
        "session.fork",
        {"session_id": "target", "source_session_id": "source"},
        "unrelated",
    )
    assert not result["ok"]
    assert bridge._sessions["target"].agent_session.messages == [
        {"role": "system", "content": "Workspace instructions"}
    ]


def test_fork_rejects_active_turn_without_replaying_history(worktree_bridge):
    bridge, output, *_ = worktree_bridge
    source = bridge._sessions["source"]
    source.active_job = stdio_bridge.BridgeJob(
        job_id="active", session_id="source", created_at="now", status="running"
    )
    try:
        result = request(
            bridge,
            output,
            "session.fork",
            {"session_id": "target", "source_session_id": "source"},
            "busy",
        )
        assert not result["ok"]
        assert result["error"]["code"] == "session_busy"
    finally:
        source.active_job = None


def test_fork_accepts_real_agent_sessions_with_fresh_workspace_instructions(
    worktree_bridge, monkeypatch
):
    bridge, output, root, target, _ = worktree_bridge
    bridge._create_session = stdio_bridge.create_session
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", api_key="test-fixture-key"),
    )
    for name, workspace in [("real-source", root), ("real-target", target)]:
        result = request(
            bridge,
            output,
            "session.create",
            {
                "session_id": name,
                "workspace": str(workspace),
                "mode": "readonly",
                "workspace_trusted": True,
            },
            name,
        )
        assert result["ok"], result
    bridge._sessions["real-source"].agent_session.messages.extend(
        [
            {"role": "user", "content": "Remember Aurora"},
            {"role": "assistant", "content": "I will remember Aurora"},
        ]
    )
    result = request(
        bridge,
        output,
        "session.fork",
        {"session_id": "real-target", "source_session_id": "real-source"},
        "real-fork",
    )
    assert result["ok"], result
    assert (
        bridge._sessions["real-target"].agent_session.messages[-1]["content"]
        == "I will remember Aurora"
    )
    request(
        bridge,
        output,
        "session.cancel",
        {"session_id": "real-target", "close_when_idle": True},
        "close-target",
    )
    for name, retained in [("resumed-once", "real-target"), ("resumed-twice", "resumed-once")]:
        created = request(
            bridge,
            output,
            "session.create",
            {
                "session_id": name,
                "workspace": str(target),
                "mode": "readonly",
                "workspace_trusted": True,
            },
            name,
        )
        assert created["ok"], created
        resumed = request(
            bridge,
            output,
            "session.resume",
            {"session_id": name, "target_session_id": retained, "emit_history": True},
            f"resume-{name}",
        )
        assert resumed["ok"], resumed
        assert any(
            message.get("content") == "I will remember Aurora"
            for message in bridge._sessions[name].agent_session.messages
        )
        visible = [
            (event["payload"].get("role"), event["payload"].get("text"))
            for event in map(json.loads, output.getvalue().splitlines())
            if event.get("type") == "message_end" and event.get("session_id") == name
        ]
        assert visible == [("user", "Remember Aurora"), ("assistant", "I will remember Aurora")]
        # Recovery context is still available to the model on repeated resume,
        # while the visible replay contains only the actual conversation.
        assert any(
            message.get("_alysis_resume_context") is True
            for message in bridge._sessions[name].agent_session.messages
        )
        request(
            bridge,
            output,
            "session.cancel",
            {"session_id": name, "close_when_idle": True},
            f"close-{name}",
        )
