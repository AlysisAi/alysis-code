"""Exercise model-led skill loading, not model selection/obedience heuristics."""

from __future__ import annotations

import base64
import socket
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.agent import session as session_mod
from alysis_code.config import AppConfig
from alysis_code.hooks.models import HookDispatchResult
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.skills import build_explicit_skill_context_message


class _ScriptedClient:
    model = "test-model"
    temperature = 0.0
    reasoning_effort = "high"
    supports_tool_calling = True

    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(
            {
                "messages": deepcopy(kwargs["messages"]),
                "tools": deepcopy(kwargs.get("tools")),
                "reasoning_effort": self.reasoning_effort,
            }
        )
        assert self.responses, "Unexpected auxiliary or repeated model call"
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, folder in (
        ("ALYSIS_DATA_DIR", "data"),
        ("ALYSIS_CONFIG_DIR", "config"),
        ("XDG_CONFIG_HOME", "xdg"),
    ):
        monkeypatch.setenv(key, str(tmp_path / folder))

    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Network/provider access is forbidden in this test")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(httpx.Client, "send", deny)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny)
    # Factories are blocked before any real session construction. Individual
    # tests replace only this boundary with their explicit scripted clients.
    monkeypatch.setattr(session_mod, "_make_session_llm_client", deny)


@pytest.fixture
def session_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sessions = []
    constructed: list[dict[str, Any]] = []

    def create(clients: Sequence[_ScriptedClient], **options: Any):
        remaining = list(clients)

        def client_factory(**kwargs: Any) -> _ScriptedClient:
            constructed.append(dict(kwargs))
            assert remaining, "Unexpected auxiliary client provisioning"
            client = remaining.pop(0)
            client.reasoning_effort = kwargs["reasoning_effort"]
            return client

        monkeypatch.setattr(session_mod, "_make_session_llm_client", client_factory)
        cfg = AppConfig(
            model="test-model",
            llm_reasoning_effort="high",
            web_search_mode="off",
            bundled_skills_enabled=False,
            skills_enabled=options.pop("skills_enabled", True),
            skills_auto_invoke=options.pop("auto", True),
            hooks_enabled=False,
        )
        session = session_mod.create_session(
            cfg=cfg,
            root=tmp_path,
            mode="readonly",
            yes=True,
            max_steps=5,
            no_log=True,
            api_key_override="unused-test-value",
            enable_compaction=False,
            verification_enabled=False,
            **options,
        )
        sessions.append(session)
        return session, constructed

    yield create
    for session in reversed(sessions):
        session.close()


def _write_skill(root: Path, name: str = "maintenance", body: str = "WORKFLOW BODY") -> None:
    bundle = root / ".alysis_skills" / name
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Inspect repository maintenance evidence.\n---\n\n{body}\n"
    )


def _response(content: str = "Done.", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=list(calls), raw={})


def _call(tool_name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=tool_name + str(arguments), name=tool_name, arguments=arguments)


def _events(session: Any, kind: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in session.store.events_snapshot() if e.get("type") == kind]


def _assert_no_selector(session: Any) -> None:
    assert not any(
        str(e.get("type", "")).startswith("skill_selection")
        for e in session.store.events_snapshot()
    )


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
@pytest.mark.parametrize("choose_skill", [False, True], ids=["direct", "read-workflow"])
def test_catalog_allows_model_chosen_read_or_direct_work(
    tmp_path: Path, session_factory, one_shot: bool, choose_skill: bool
) -> None:
    _write_skill(tmp_path)
    (tmp_path / "evidence.txt").write_text("ACTUAL FILE EVIDENCE")
    chosen = (
        _call("skill_read", name="maintenance")
        if choose_skill
        else _call("fs_read", path="evidence.txt")
    )
    client = _ScriptedClient([_response("", chosen), _response()])
    session, factories = session_factory([client], one_shot_execution=one_shot)
    assert session.run_turn("Inspect the maintenance evidence and explain it.") == 0
    initial = str(client.calls[0]["messages"])
    catalog = next(
        m["content"]
        for m in client.calls[0]["messages"]
        if str(m.get("content", "")).startswith("<skill_context>")
    )
    assert "maintenance" in catalog and "Inspect repository maintenance evidence" in catalog
    assert "Otherwise continue directly" in catalog
    assert "WORKFLOW BODY" not in initial
    assert "<semantic_skill_selection>" not in initial
    assert len(client.calls) == 2 and len(factories) == 1
    assert all(call["reasoning_effort"] == "high" for call in client.calls)
    assert ("WORKFLOW BODY" in str(client.calls[-1]["messages"])) is choose_skill
    assert [p["name"] for p in _events(session, "tool_call")] == [chosen.name]
    _assert_no_selector(session)


@pytest.mark.parametrize("auto", [False, True])
def test_named_skill_and_independent_read_load_together_without_write_privilege(
    tmp_path: Path, session_factory, auto: bool
) -> None:
    _write_skill(tmp_path)
    (tmp_path / "evidence.txt").write_text("INDEPENDENT EVIDENCE")
    client = _ScriptedClient(
        [
            _response(
                "", _call("skill_read", name="maintenance"), _call("fs_read", path="evidence.txt")
            ),
            _response("", _call("fs_write", path="forbidden.txt", content="not allowed")),
            _response("The read-only evidence is available."),
        ]
    )
    session, factories = session_factory([client], auto=auto)
    assert session.run_turn("Use the maintenance skill and inspect evidence.txt; do not edit.") == 0
    assert "WORKFLOW BODY" in str(client.calls[1]["messages"])
    assert "INDEPENDENT EVIDENCE" in str(client.calls[1]["messages"])
    assert not (tmp_path / "forbidden.txt").exists()
    assert len(factories) == 1
    if not auto:
        assert "Skills are optional attachable context" in str(client.calls[0]["messages"])
    _assert_no_selector(session)


@pytest.mark.parametrize("auto", [False, True])
def test_explicit_attachment_is_request_only_without_selector(
    tmp_path: Path, session_factory, auto: bool
) -> None:
    _write_skill(tmp_path)
    client = _ScriptedClient(
        [_response("Attached instructions used."), _response("Separate answer.")]
    )
    session, factories = session_factory([client], auto=auto)
    skill = next(s for s in session.skills_ordered if s.name == "maintenance")
    attachment = build_explicit_skill_context_message(skill=skill, task_text="Explain it.")
    assert (
        session.run_turn("Explain the supplied skill.", ephemeral_user_messages=[attachment]) == 0
    )
    assert session.run_turn("Now answer a separate question.") == 0
    assert "WORKFLOW BODY" in str(client.calls[0]["messages"])
    assert "<explicit_skill_context>" not in str(client.calls[1]["messages"])
    assert "WORKFLOW BODY" not in str(session.messages)
    assert not _events(session, "tool_call")
    assert len(factories) == 1
    _assert_no_selector(session)


def test_followup_reuses_loaded_workflow_without_extra_read(
    tmp_path: Path, session_factory
) -> None:
    _write_skill(tmp_path)
    client = _ScriptedClient(
        [
            _response("", _call("skill_read", name="maintenance")),
            _response("Loaded."),
            _response("Reused."),
        ]
    )
    session, factories = session_factory([client])
    assert session.run_turn("Read the maintenance instructions.") == 0
    assert session.run_turn("Use those instructions for the next explanation.") == 0
    assert "WORKFLOW BODY" in str(client.calls[-1]["messages"])
    assert len(_events(session, "tool_call")) == 1
    assert len(client.calls) == 3 and len(factories) == 1
    _assert_no_selector(session)


def test_image_reaches_main_unchanged_without_auxiliary_text_call(
    tmp_path: Path, session_factory
) -> None:
    _write_skill(tmp_path)
    image = tmp_path / "sample.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    client = _ScriptedClient([_response("Image received.")])
    session, factories = session_factory([client])
    assert session.run_turn("Inspect the image.", image_paths=["sample.png"]) == 0
    parts = [
        p
        for m in client.calls[0]["messages"]
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p.get("type") == "image_url"
    ]
    assert len(parts) == 1
    assert base64.b64decode(parts[0]["image_url"]["url"].split(",", 1)[1]) == image.read_bytes()
    assert len(client.calls) == len(factories) == 1
    assert client.reasoning_effort == "high"
    _assert_no_selector(session)


def test_disabled_skills_do_not_publish_catalog_or_read_tool(
    tmp_path: Path, session_factory
) -> None:
    _write_skill(tmp_path)
    client = _ScriptedClient([_response()])
    session, factories = session_factory([client], skills_enabled=False)
    assert session.run_turn("Explain the repository.") == 0
    assert "<skill_context>" not in str(client.calls[0]["messages"])
    assert "skill_read" not in session.tools
    assert len(factories) == 1
    _assert_no_selector(session)


class _MutateReadHook:
    def __init__(self, arguments: dict[str, Any], *, blocked: bool = False) -> None:
        self.arguments, self.blocked = arguments, blocked

    def fire_pre_tool_use(self, **kwargs: Any) -> HookDispatchResult:
        if kwargs.get("tool_name") == "skill_read":
            return HookDispatchResult(blocked=self.blocked, modified_input=self.arguments)
        return HookDispatchResult()

    def __getattr__(self, name: str):
        if name.startswith("fire_"):
            return lambda **_kwargs: HookDispatchResult()
        raise AttributeError(name)


@pytest.mark.parametrize(
    "arguments,blocked",
    [
        ({"name": "missing"}, False),
        ({"name": "maintenance", "path": "../../outside.txt"}, False),
        ({"name": "maintenance"}, True),
    ],
)
def test_model_skill_choice_preserves_registry_bundle_and_hook_guards(
    tmp_path: Path, session_factory, arguments: dict[str, Any], blocked: bool
) -> None:
    _write_skill(tmp_path)
    (tmp_path / "outside.txt").write_text("OUTSIDE BUNDLE")
    client = _ScriptedClient(
        [_response("", _call("skill_read", name="maintenance")), _response("Unavailable.")]
    )
    session, _ = session_factory([client])
    session.hook_dispatcher = _MutateReadHook(arguments, blocked=blocked)
    assert session.run_turn("Read the maintenance instructions.") == 0
    assert "error" in _events(session, "tool_result")[0]["result"]
    assert "WORKFLOW BODY" not in str(client.calls[-1]["messages"])
    assert "OUTSIDE BUNDLE" not in str(client.calls[-1]["messages"])
    _assert_no_selector(session)


def test_real_readonly_child_uses_same_catalog_and_reasoning_without_selector(
    tmp_path: Path, session_factory
) -> None:
    _write_skill(tmp_path)
    parent_client = _ScriptedClient([])
    child_client = _ScriptedClient(
        [_response("", _call("skill_read", name="maintenance")), _response("Reviewed.")]
    )
    parent, factories = session_factory([parent_client, child_client], subagents_enabled=True)
    parent.messages.append({"role": "user", "content": "UNASSIGNED PARENT HISTORY"})
    result = parent.tools["subagent_run"].run(
        {
            "name": "general",
            "mode": "readonly",
            "task": "Inspect the maintenance workflow; no edits.",
        }
    )
    assert "error" not in result, result
    assert result["status"] == "success" and result["result"] == "Reviewed.", result
    assert len(factories) == 2 and not parent_client.calls
    assert all(item["reasoning_effort"] == "high" for item in factories)
    assert all(call["reasoning_effort"] == "high" for call in child_client.calls)
    assert "<skill_context>" in str(child_client.calls[0]["messages"])
    assert "WORKFLOW BODY" not in str(child_client.calls[0]["messages"])
    assert "WORKFLOW BODY" in str(child_client.calls[-1]["messages"])
    assert "UNASSIGNED PARENT HISTORY" not in str(child_client.calls[0]["messages"])
    names = {t["function"]["name"] for t in child_client.calls[0]["tools"]}
    assert "skill_read" in names and "fs_write" not in names
    _assert_no_selector(parent)
