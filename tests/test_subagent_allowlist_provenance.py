from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_subagents import (
    _build_main_tools,
    _FakeSubSession,
    _readonly_subagent_tools,
    _RecordingStore,
)

from alysis_code import agent_loop
from alysis_code import subagents as subagents_mod
from alysis_code.subagents import SubagentDefinition, built_in_subagents, load_subagent_registry


def _write_role(directory: Path, meta: dict[str, Any], body: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}: {json.dumps(value)}" for key, value in meta.items()]
    (directory / f"{meta['name']}.md").write_text(
        "---\n" + "\n".join(lines) + "\n---\n" + body, encoding="utf-8"
    )


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *directories: Path):
    monkeypatch.setattr(
        subagents_mod, "_candidate_agent_directories", lambda **_kwargs: list(directories)
    )
    return load_subagent_registry(root=tmp_path)


@pytest.mark.parametrize("allow_key", ["allow_tools", "tools_allow", "tools"])
def test_metadata_allowlist_records_explicit_intent_without_changing_prompt_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_key: str
) -> None:
    directory = tmp_path / "roles"
    _write_role(directory, {"name": "explorer", allow_key: ["fs_read", "missing_probe"]})
    role = _load(tmp_path, monkeypatch, directory)["explorer"]
    builtin = built_in_subagents()["explorer"]
    assert role.allow_tools == ("fs_read", "missing_probe")
    assert role.allow_tools_explicit is True
    assert builtin.allow_tools_explicit is False
    assert role.prompt_trust == builtin.prompt_trust == "trusted"
    assert role.system_prompt == builtin.system_prompt
    assert role.mode == builtin.mode == "readonly"


def test_metadata_without_allowlist_inherits_builtin_or_prior_explicit_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_role(
        second, {"name": "explorer", "model": "configured-worker", "deny_tools": ["fs_read"]}
    )
    inherited = _load(tmp_path, monkeypatch, second)["explorer"]
    builtin = built_in_subagents()["explorer"]
    assert inherited.allow_tools == builtin.allow_tools
    assert inherited.allow_tools_explicit is False
    assert inherited.deny_tools == ("fs_read",)

    _write_role(first, {"name": "explorer", "allow_tools": ["fs_read", "missing_probe"]})
    layered = _load(tmp_path, monkeypatch, first, second)["explorer"]
    assert layered.allow_tools == ("fs_read", "missing_probe")
    assert layered.allow_tools_explicit is True
    assert layered.prompt_trust == "trusted"
    assert layered.model == "configured-worker"


def test_allowlist_provenance_cannot_be_disabled_by_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "roles"
    _write_role(
        directory,
        {"name": "explorer", "allow_tools": ["missing_probe"], "allow_tools_explicit": False},
    )
    assert _load(tmp_path, monkeypatch, directory)["explorer"].allow_tools_explicit is True


@pytest.mark.parametrize("body", ["", "Read the assigned source using only the configured tools."])
def test_explicit_unavailable_entry_fails_before_model_call_for_both_prompt_trusts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    directory = tmp_path / "roles"
    _write_role(directory, {"name": "explorer", "allow_tools": ["fs_read", "missing_probe"]}, body)
    registry = _load(tmp_path, monkeypatch, directory)
    assert registry["explorer"].prompt_trust == ("untrusted" if body else "trusted")
    child = _FakeSubSession(tools=_readonly_subagent_tools())
    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: child)
    store = _RecordingStore()
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry=registry, store=store
    )
    result = tools["subagent_run"].run({"name": "explorer", "task": "Inspect the assigned source."})
    assert result["error_code"] == "subagent_allowlist_unavailable"
    assert result["unavailable_allowed_tools"] == ["missing_probe"]
    assert result["resolved_allowed_tools"] == ["fs_read"]
    assert "Correct the configured allowlist" in result["error"]
    assert child.run_calls == []
    assert child.closed is True
    assert not [event for event in store.events if event[0] == "subagent_tool_catalog"]


def test_programmatically_authored_trusted_allowlist_is_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    role = SubagentDefinition(
        name="trusted-reader",
        description="Read source.",
        system_prompt="Trusted authored instructions.",
        allow_tools=("fs_read", "missing_probe"),
    )
    child = _FakeSubSession(tools=_readonly_subagent_tools())
    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: child)
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry={role.name: role}
    )
    result = tools["subagent_run"].run({"name": role.name, "task": "Inspect source."})
    assert result["error_code"] == "subagent_allowlist_unavailable"
    assert child.run_calls == []


@pytest.mark.parametrize("configured_model", [False, True])
def test_builtin_optional_tool_surface_still_narrows_to_available_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_model: bool
) -> None:
    registry = built_in_subagents()
    if configured_model:
        directory = tmp_path / "roles"
        _write_role(directory, {"name": "explorer", "model": "configured-worker"})
        registry = _load(tmp_path, monkeypatch, directory)
    assert registry["explorer"].allow_tools_explicit is False
    child = _FakeSubSession(tools=_readonly_subagent_tools())
    assert set(registry["explorer"].allow_tools) - set(child.tools)
    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: child)
    tools = _build_main_tools(tmp_path=tmp_path, subagents_enabled=True, subagent_registry=registry)
    result = tools["subagent_run"].run({"name": "explorer", "task": "Inspect source."})
    assert result["status"] == "success"
    assert result["sandbox"]["tools"] == ["fs_read"]
    assert child.run_calls == ["Inspect source."]


def test_explicit_allowlist_retains_deny_precedence_and_readonly_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "roles"
    _write_role(
        directory,
        {"name": "explorer", "allow_tools": ["fs_read", "fs_list"], "deny_tools": ["fs_read"]},
    )
    registry = _load(tmp_path, monkeypatch, directory)
    child = _FakeSubSession(
        tools=_build_main_tools(tmp_path=tmp_path, subagents_enabled=False, mode="readonly")
    )
    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: child)
    tools = _build_main_tools(tmp_path=tmp_path, subagents_enabled=True, subagent_registry=registry)
    result = tools["subagent_run"].run({"name": "explorer", "task": "Locate the source."})
    assert result["status"] == "success"
    assert result["sandbox"]["tools"] == ["fs_list"]
    assert not {"fs_read", "fs_write", "shell_run"}.intersection(child.tools)


def test_explicit_empty_allowlist_keeps_existing_unrestricted_then_deny_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "roles"
    _write_role(directory, {"name": "explorer", "allow_tools": [], "deny_tools": ["fs_read"]})
    role = _load(tmp_path, monkeypatch, directory)["explorer"]
    assert role.allow_tools_explicit is True
    scope = subagents_mod.resolve_subagent_tool_scope(
        tool_names=["fs_read", "fs_list"], allow_tools=role.allow_tools, deny_tools=role.deny_tools
    )
    assert scope.allowed_names == ("fs_list",)
    assert scope.unavailable_allowed_tools == ()


def test_resume_revalidates_current_explicit_metadata_allowlist_then_can_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "roles"
    _write_role(directory, {"name": "explorer", "allow_tools": ["fs_read"]})
    registry = _load(tmp_path, monkeypatch, directory)
    children: list[_FakeSubSession] = []

    class Child(_FakeSubSession):
        def __init__(self, index: int) -> None:
            super().__init__(
                tools=_readonly_subagent_tools(),
                messages=[{"role": "system", "content": "Child instructions."}],
                store_events=[],
                session_id=f"reader-{index}",
            )
            self.index = index

        def run_turn(self, task: str, *, cancellation_token: Any = None) -> int:
            result = super().run_turn(task, cancellation_token=cancellation_token)
            report = f"Report from executed child {self.index}."
            self.messages.extend(
                [{"role": "user", "content": task}, {"role": "assistant", "content": report}]
            )
            self.store._events = [
                {"type": "user_message", "payload": {"content": task}},
                {"type": "assistant_message", "payload": {"content": report}},
                {"type": "final", "payload": {"content": report}},
            ]
            return result

    def create_child(**_kwargs: Any) -> _FakeSubSession:
        child = Child(len(children))
        children.append(child)
        return child

    monkeypatch.setattr(agent_loop, "create_session", create_child)
    store = _RecordingStore()
    # A pre-model failure has no child events, so resume may consult the parent
    # store's log directory before falling back to retained in-memory history.
    store.sessions_dir = tmp_path / "sessions"
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry=registry, store=store
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        first = tools["subagent_run"].run({"name": "explorer", "task": "Inspect source."})
        assert first["status"] == "success"
        _write_role(directory, {"name": "explorer", "allow_tools": ["fs_read", "missing_probe"]})
        registry["explorer"] = _load(tmp_path, monkeypatch, directory)["explorer"]
        second = tools["subagent_resume"].run({"run_id": first["run_id"], "task": "Review more."})
        second_result = scheduler._children[second["run_id"]].completion.result(timeout=5)
        scheduler._children[second["run_id"]].worker_bookkeeping_completion.result(timeout=5)
        assert second_result["error_code"] == "subagent_allowlist_unavailable"
        assert children[1].run_calls == []
        assert children[1].closed is True

        _write_role(directory, {"name": "explorer", "allow_tools": ["fs_read"]})
        registry["explorer"] = _load(tmp_path, monkeypatch, directory)["explorer"]
        recovered = tools["subagent_resume"].run(
            {"run_id": second["run_id"], "task": "Continue the source review."}
        )
        recovered_result = scheduler._children[recovered["run_id"]].completion.result(timeout=5)
        scheduler._children[recovered["run_id"]].worker_bookkeeping_completion.result(timeout=5)
        assert recovered_result["status"] == "success"
        assert recovered_result["sandbox"]["tools"] == ["fs_read"]
        assert children[2].run_calls == ["Continue the source review."]
        assert any(
            message.get("content") == "Report from executed child 0."
            for message in children[2].messages
        )
        assert not any(
            message.get("content") == "Report from executed child 1."
            for message in children[2].messages
        )
    finally:
        scheduler.shutdown(cancel_pending=True)
