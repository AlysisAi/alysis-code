from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_subagents import (
    _build_main_tools,
    _FakeSubSession,
    _readonly_subagent_tools,
    _RecordingStore,
)

from alysis_code import agent_loop, config
from alysis_code.agent.subagent_execution import SubagentLauncher
from alysis_code.config import AppConfig, ConfigError
from alysis_code.profiles import ProfileSpec, add_profile, get_active_profile, set_active_profile
from alysis_code.subagents import built_in_subagents, load_subagent_registry


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "load_persisted_profile_keys", lambda: {})
    monkeypatch.setattr(config, "load_persisted_api_key", lambda: None)
    for name in ("ALYSIS_API_KEY", "OPENAI_API_KEY", "CHILD_PROFILE_KEY", "ALYSIS_MODEL_REVIEW"):
        monkeypatch.delenv(name, raising=False)


def profiles(*, child_auth: bool = False) -> AppConfig:
    cfg = AppConfig(model="parent-model", stream=False)
    add_profile(
        cfg,
        ProfileSpec(
            name="parent",
            base_url="https://parent.example/v1",
            default_model="parent-model",
            reasoning_effort="high",
        ),
    )
    add_profile(
        cfg,
        ProfileSpec(
            name="child",
            base_url="https://child.example/v1",
            default_model="child-model",
            reasoning_effort="xhigh",
            api_key_env="CHILD_PROFILE_KEY",
            auth_provider="openai-codex" if child_auth else None,
            protocol="openai_responses" if child_auth else "openai_compat",
        ),
    )
    set_active_profile(cfg, "parent")
    return cfg


@pytest.mark.parametrize("custom_body", [False, True])
def test_frontmatter_profile_is_loaded_without_changing_builtin_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_body: bool,
) -> None:
    monkeypatch.setattr("alysis_code.subagents.user_config_dir", lambda *_: str(tmp_path / "user"))
    agents = tmp_path / ".alysis_agents"
    agents.mkdir()
    (agents / "explorer.md").write_text(
        "---\nname: explorer\nprofile: child\nmodel: child-model\n---\n"
        + ("Inspect the given source." if custom_body else ""),
        encoding="utf-8",
    )
    definition = load_subagent_registry(root=tmp_path)["explorer"]
    assert definition.profile == "child"
    assert definition.model == "child-model"
    if not custom_body:
        assert replace(definition, profile=None, model=None) == built_in_subagents()["explorer"]
    else:
        assert definition.prompt_trust == "untrusted"


@pytest.mark.parametrize("source", ["stored", "env"])
def test_foreign_profile_selects_own_credentials_before_model_resolution(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    cfg = profiles()
    before = cfg.model_dump()
    if source == "stored":
        monkeypatch.setattr(config, "load_persisted_profile_keys", lambda: {"child": "child-key"})
    else:
        monkeypatch.setenv("CHILD_PROFILE_KEY", "child-key")
    child, key, role = SubagentLauncher._resolve_subagent_config(
        replace(built_in_subagents()["code-reviewer"], profile="child"),
        cfg,
        api_key="parent-key",
    )
    assert child.model == "child-model"
    assert get_active_profile(child).name == "child"
    assert child.base_url == "https://child.example/v1"
    assert child.llm_reasoning_effort == "xhigh"
    assert child.llm_enable_thinking is True
    assert key == "child-key"
    assert role == "review"
    assert cfg.model_dump() == before


def test_foreign_profile_never_uses_legacy_or_parent_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = profiles()
    monkeypatch.setattr(config, "load_persisted_api_key", lambda: "parent-legacy-key")
    monkeypatch.setenv("ALYSIS_API_KEY", "global-key")
    with pytest.raises(ConfigError, match="needs its own stored key"):
        SubagentLauncher._resolve_subagent_config(
            replace(built_in_subagents()["explorer"], profile="child"),
            cfg,
            api_key="parent-override-key",
        )


@pytest.mark.parametrize("selected_profile", [None, "parent", " PARENT "])
@pytest.mark.parametrize("explicit_model", [None, "specialist"])
def test_inherited_or_same_profile_keeps_parent_override(
    selected_profile: str | None,
    explicit_model: str | None,
) -> None:
    cfg = profiles()
    cfg.model = "invocation-model"
    cfg.llm_reasoning_effort = "xhigh"
    cfg.llm_enable_thinking = True
    before = cfg.model_dump()
    child, key, _ = SubagentLauncher._resolve_subagent_config(
        replace(built_in_subagents()["explorer"], profile=selected_profile, model=explicit_model),
        cfg,
        api_key="parent-override-key",
    )
    assert get_active_profile(child).name == "parent"
    assert child.model == (explicit_model or "invocation-model")
    assert child.llm_reasoning_effort == "xhigh"
    assert child.llm_enable_thinking is True
    assert key == "parent-override-key"
    assert cfg.model_dump() == before


def test_same_subscription_profile_keeps_invocation_model_and_reasoning() -> None:
    cfg = profiles(child_auth=True)
    set_active_profile(cfg, "child")
    cfg.model = "invocation-subscription-model"
    cfg.llm_reasoning_effort = "high"
    cfg.llm_enable_thinking = True
    child, key, _ = SubagentLauncher._resolve_subagent_config(
        replace(built_in_subagents()["explorer"], profile="child"), cfg
    )
    assert get_active_profile(child).auth_provider == "openai-codex"
    assert child.model == "invocation-subscription-model"
    assert child.llm_reasoning_effort == "high"
    assert child.llm_enable_thinking is True
    assert key is None


def test_foreign_subscription_uses_provider_auth_without_parent_static_key() -> None:
    child, key, _ = SubagentLauncher._resolve_subagent_config(
        replace(built_in_subagents()["explorer"], profile="child", model="child-model"),
        profiles(child_auth=True),
        api_key="parent-key",
    )
    assert get_active_profile(child).auth_provider == "openai-codex"
    assert key is None


def test_foreign_unset_defaults_do_not_inherit_parent_model_or_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = profiles()
    add_profile(
        cfg,
        ProfileSpec(
            name="child", base_url="https://child.example/v1", api_key_env="CHILD_PROFILE_KEY"
        ),
    )
    monkeypatch.setenv("CHILD_PROFILE_KEY", "child-key")
    definition = replace(built_in_subagents()["explorer"], profile="child")
    with pytest.raises(ConfigError, match="Model is not set"):
        SubagentLauncher._resolve_subagent_config(definition, cfg, api_key="parent-key")
    child, _, _ = SubagentLauncher._resolve_subagent_config(
        replace(definition, model="selected-child"), cfg, api_key="parent-key"
    )
    assert child.model == "selected-child"
    assert child.llm_reasoning_effort is None
    assert child.llm_enable_thinking is None


def test_foreign_empty_endpoint_cannot_receive_child_key_at_parent_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = profiles()
    add_profile(cfg, ProfileSpec(name="child", default_model="child-model"))
    monkeypatch.setattr(config, "load_persisted_profile_keys", lambda: {"child": "child-key"})
    with pytest.raises(ConfigError, match="needs an explicit base URL"):
        SubagentLauncher._resolve_subagent_config(
            replace(built_in_subagents()["explorer"], profile="child"),
            cfg,
            api_key="parent-key",
        )
    assert cfg.base_url == "https://parent.example/v1"


@pytest.mark.parametrize("kind", ["sync", "background", "helper"])
@pytest.mark.parametrize("same_profile", [False, True])
def test_native_launch_uses_selected_profile_and_same_route_on_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    same_profile: bool,
) -> None:
    monkeypatch.setenv("CHILD_PROFILE_KEY", "child-key")
    captured: list[dict[str, Any]] = []

    def create_child(**kwargs: Any) -> _FakeSubSession:
        captured.append(kwargs)
        return _FakeSubSession(
            tools=_readonly_subagent_tools(), session_id=f"child-{len(captured)}"
        )

    monkeypatch.setattr(agent_loop, "create_session", create_child)
    store = _RecordingStore()
    cfg = profiles()
    cfg.model = "invocation-model"
    cfg.llm_reasoning_effort = "low"
    cfg.llm_enable_thinking = True
    selected_profile = "parent" if same_profile else "child"
    expected_model = "invocation-model" if same_profile else "child-model"
    expected_effort = "low" if same_profile else "xhigh"
    expected_key = "parent-key" if same_profile else "child-key"
    tools = _build_main_tools(
        tmp_path=tmp_path,
        subagents_enabled=True,
        cfg=cfg,
        api_key="parent-key",
        store=store,
        subagent_registry={
            "explorer": replace(built_in_subagents()["explorer"], profile=selected_profile),
        },
    )
    launcher = tools["subagent_run"].run.__self__
    scheduler = launcher.child_scheduler
    if kind == "helper":
        launcher.helper_only = True
        launcher.subagent_depth = 1
        launcher.helper_allowed_names = ("explorer",)
    try:
        tool = "subagent_spawn" if kind == "background" else "subagent_run"
        first = tools[tool].run({"name": "explorer", "task": "Inspect source."})
        if kind == "background":
            result = tools["subagent_wait"].run({"run_id": first["run_id"]})["results"][
                first["run_id"]
            ]
        else:
            result = first
        assert result.get("status") == "success", result
        assert result["profile_name"] == selected_profile
        assert result["model"] == expected_model
        assert result["protocol"] == "openai_compat"
        assert result["auth_provider"] is None
        if kind != "helper":
            # A retained successful child's history is restored through the same
            # native route selection; no stale parent key is forwarded.
            child = scheduler._children[first["run_id"]]
            child.worker_bookkeeping_completion.result(timeout=5)
            resumed = tools["subagent_resume"].run(
                {"run_id": first["run_id"], "task": "Check one more path."}
            )
            followup = tools["subagent_wait"].run({"run_id": resumed["run_id"]})["results"][
                resumed["run_id"]
            ]
            assert followup["status"] == "success"
            assert len(captured) == 2
        for launch in captured:
            assert get_active_profile(launch["cfg"]).name == selected_profile
            assert launch["cfg"].model == expected_model
            assert launch["cfg"].llm_reasoning_effort == expected_effort
            assert launch["cfg"].llm_enable_thinking is True
            assert launch["api_key_override"] == expected_key
        starts = [p for kind, p in store.events if kind == "subagent_start"]
        assert starts and all(p["profile_name"] == selected_profile for p in starts)
        assert "parent-key" not in str(starts)
        assert "child-key" not in str(starts)
    finally:
        scheduler.shutdown(cancel_pending=True)


@pytest.mark.parametrize("kind", ["sync", "background", "helper"])
@pytest.mark.parametrize("source", ["stored", "env", "subscription"])
def test_keyless_parent_launches_child_with_its_own_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    source: str,
) -> None:
    cfg = profiles(child_auth=source == "subscription")
    add_profile(
        cfg,
        ProfileSpec(name="parent", base_url="http://localhost:8000/v1", default_model="local"),
    )
    set_active_profile(cfg, "parent")
    before = cfg.model_dump()
    if source == "stored":
        monkeypatch.setattr(config, "load_persisted_profile_keys", lambda: {"child": "child-key"})
    elif source == "env":
        monkeypatch.setenv("CHILD_PROFILE_KEY", "child-key")
    captured: list[dict[str, Any]] = []

    def create_child(**kwargs: Any) -> _FakeSubSession:
        captured.append(kwargs)
        return _FakeSubSession(tools=_readonly_subagent_tools(), session_id="child")

    monkeypatch.setattr(agent_loop, "create_session", create_child)
    tools = _build_main_tools(
        tmp_path=tmp_path,
        subagents_enabled=True,
        cfg=cfg,
        api_key=None if source == "subscription" else "",
        subagent_registry={"explorer": replace(built_in_subagents()["explorer"], profile="child")},
    )
    launcher = tools["subagent_run"].run.__self__
    if kind == "helper":
        launcher.helper_only = True
        launcher.subagent_depth = 1
        launcher.helper_allowed_names = ("explorer",)
    try:
        tool = "subagent_spawn" if kind == "background" else "subagent_run"
        result = tools[tool].run({"name": "explorer", "task": "Inspect source."})
        assert "error" not in result, result
        if kind == "background":
            result = tools["subagent_wait"].run({"run_id": result["run_id"]})["results"][
                result["run_id"]
            ]
        assert result["status"] == "success", result
        assert len(captured) == 1
        launch = captured[0]
        assert get_active_profile(launch["cfg"]).name == "child"
        assert launch["cfg"].base_url == "https://child.example/v1"
        assert launch["cfg"].model == "child-model"
        assert launch["api_key_override"] == (None if source == "subscription" else "child-key")
        assert result["auth_provider"] == ("openai-codex" if source == "subscription" else None)
        assert cfg.model_dump() == before
    finally:
        launcher.child_scheduler.shutdown(cancel_pending=True)


@pytest.mark.parametrize("parent_key", ["parent-key", ""])
@pytest.mark.parametrize("profile", ["missing", "child"])
def test_bad_profile_fails_before_background_registration_or_session_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    parent_key: str,
) -> None:
    def unexpected(**_kwargs: Any) -> None:
        pytest.fail("Invalid profile must not create a session")

    monkeypatch.setattr(agent_loop, "create_session", unexpected)
    tools = _build_main_tools(
        tmp_path=tmp_path,
        subagents_enabled=True,
        cfg=profiles(),
        api_key=parent_key,
        subagent_registry={"explorer": replace(built_in_subagents()["explorer"], profile=profile)},
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        for tool in ("subagent_spawn", "subagent_run"):
            result = tools[tool].run({"name": "explorer", "task": "Inspect source."})
            assert result["error_code"] == "subagent_profile_resolution_failed"
            if tool == "subagent_spawn":
                assert not scheduler._children
    finally:
        scheduler.shutdown(cancel_pending=True)


def test_inherited_profile_still_requires_credentials() -> None:
    with pytest.raises(ConfigError, match="API key|authentication"):
        SubagentLauncher._resolve_subagent_config(built_in_subagents()["explorer"], profiles())
