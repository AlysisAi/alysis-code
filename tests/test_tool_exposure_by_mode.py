from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent_loop import _tool_event_metadata, build_tools
from alysis_code.config import AppConfig
from alysis_code.custom_tools.discovery import discover_custom_tools
from alysis_code.custom_tools.trust import trust_project_tool
from alysis_code.mcp.manager import McpHostToolBinding, McpToolBinding
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface


def _store(root: Path, *, enabled: bool = True) -> SessionStore:
    return SessionStore(
        enabled=enabled,
        sessions_dir=root / "sessions",
        session_id="tool-exposure-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _fake_git_repo(root: Path) -> None:
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("0" * 40 + "\n", encoding="utf-8")


class _FakeMcpClient:
    def __init__(self) -> None:
        self.called = False

    def call_tool(self, *, tool_name: str, arguments: dict[str, object]) -> SimpleNamespace:
        self.called = True
        return SimpleNamespace(
            is_error=False,
            content=[{"type": "text", "text": f"ok:{tool_name}:{arguments!r}"}],
            content_summary="ok",
            structured_content=None,
            extracted_text="ok",
        )


class _DummyMcpManager:
    def __init__(self, *bindings: McpToolBinding) -> None:
        self.tool_bindings = tuple(bindings)


class _RecordingSurface(NoopSurface):
    def __init__(self, *, allow: bool = True) -> None:
        super().__init__()
        self.allow = allow
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)


def _make_mcp_binding(*, session_mode: str | None = None) -> tuple[McpToolBinding, _FakeMcpClient]:
    client = _FakeMcpClient()
    binding = McpToolBinding(
        server_id="alpha",
        tool_name="echo",
        tool_alias="mcp__alpha__echo",
        description="Echo via MCP",
        parameters={"type": "object", "properties": {}, "required": []},
        client=client,
        session_mode=session_mode,
    )
    return binding, client


def _make_mcp_host_binding(
    *,
    session_mode: str | None = None,
) -> tuple[McpHostToolBinding, dict[str, bool]]:
    state = {"called": False}

    def _run_handler(_arguments: dict[str, object]) -> dict[str, object]:
        state["called"] = True
        return {"ok": True}

    binding = McpHostToolBinding(
        tool_name="mcp_resource_read",
        tool_alias="mcp_resource_read",
        description="Read resource via MCP",
        parameters={"type": "object", "properties": {}, "required": []},
        run_handler=_run_handler,
        session_mode=session_mode,
    )
    return binding, state


def _write_project_custom_tool(
    root: Path,
    *,
    name: str = "project_echo",
    body: str = "return {'ok': True}",
    extra_manifest_lines: list[str] | None = None,
) -> None:
    extra_manifest_lines = list(extra_manifest_lines or [])
    tool_path = root / ".alysis" / "tools" / f"{name}.py"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "TOOL = {",
        f'    "name": "{name}",',
        '    "description": "Project custom tool",',
        '    "input_schema": {"type": "object", "properties": {}, "required": []},',
    ]
    manifest_lines.extend(f"    {line}" for line in extra_manifest_lines)
    manifest_lines.append("}")
    tool_path.write_text(
        "\n".join(
            [
                *manifest_lines,
                "",
                "def run(args):",
                f"    {body}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _trust_project_custom_tool(root: Path) -> None:
    discovered = discover_custom_tools(
        workspace_root=root,
        built_in_tool_names=set(),
    )
    [spec] = discovered.project_tools
    trust_project_tool(spec)


def _build_tools(
    tmp_path: Path,
    *,
    mode: str,
    runtime_kind: RuntimeKind = RuntimeKind.INTERACTIVE_CHAT,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    mcp_manager: object | None = None,
    non_interactive: bool = True,
    surface: object | None = None,
    cfg: AppConfig | None = None,
    yes: bool = True,
) -> dict[str, object]:
    _fake_git_repo(tmp_path)
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=_store(tmp_path, enabled=True),
        mode=mode,
        yes=yes,
        cfg=cfg
        or AppConfig(
            model="test-model",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=non_interactive,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        verification_enabled=True,
        subagents_enabled=True,
        subagent_registry={},
        runtime_kind=runtime_kind,
        mcp_manager=mcp_manager,  # type: ignore[arg-type]
    )


def test_build_tools_readonly_excludes_mutating_and_higher_risk_tools(tmp_path: Path) -> None:
    binding, _client = _make_mcp_binding()
    tools = _build_tools(
        tmp_path,
        mode="readonly",
        mcp_manager=_DummyMcpManager(binding),
    )

    for name in (
        "fs_write",
        "fs_edit",
        "fs_move",
        "fs_copy",
        "fs_delete",
        "fs_mkdir",
        "shell_run",
        "verify_run",
        "git_apply_patch",
        "subagent_run",
        "mcp__alpha__echo",
    ):
        assert name not in tools


def test_build_tools_readonly_keeps_expected_read_safe_inspection_tools(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path, mode="readonly")

    for name in (
        "fs_read",
        "fs_read_lines",
        "fs_list",
        "search_rg",
        "symbol_search",
        "repo_map",
        "history_search",
        "session_artifact_read",
        "web_fetch",
        "web_search",
        "git_status",
        "git_diff",
        "git_history",
        "web_fetch",
        "web_search",
    ):
        assert name in tools


def test_build_tools_readonly_does_not_construct_shell_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build_shell_runner(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("readonly mode should not construct a shell runner")

    monkeypatch.setattr(agent_loop_mod, "build_shell_runner", fail_build_shell_runner)

    tools = _build_tools(tmp_path, mode="readonly")

    assert "shell_run" not in tools


@pytest.mark.parametrize("mode", ["review", "auto", "fullaccess"])
def test_build_tools_write_capable_modes_keep_expected_builtin_and_mcp_surface(
    tmp_path: Path,
    mode: str,
) -> None:
    binding, _client = _make_mcp_binding()
    tools = _build_tools(
        tmp_path,
        mode=mode,
        mcp_manager=_DummyMcpManager(binding),
    )

    for name in (
        "fs_write",
        "fs_edit",
        "fs_move",
        "fs_copy",
        "fs_delete",
        "fs_mkdir",
        "shell_run",
        "verify_run",
        "git_apply_patch",
        "subagent_run",
        "web_fetch",
        "web_search",
        "mcp__alpha__echo",
    ):
        assert name in tools


def test_auto_mode_fs_delete_requires_approval_when_not_yes(tmp_path: Path) -> None:
    target = tmp_path / "old.txt"
    target.write_text("old\n", encoding="utf-8")
    surface = _RecordingSurface(allow=True)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        non_interactive=False,
        surface=surface,
        yes=False,
    )

    result = tools["fs_delete"].run({"path": "old.txt"})

    assert result["deleted"] is True
    assert target.exists() is False
    assert [request.kind for request in surface.requests] == ["fs_delete"]
    assert "file deletion" in surface.requests[0].reason
    assert surface.requests[0].files == ["old.txt"]


def test_mcp_tool_binding_rejects_readonly_session_mode_before_server_call() -> None:
    binding, client = _make_mcp_binding(session_mode="readonly")

    with pytest.raises(RuntimeError, match="Blocked in readonly mode: MCP tool"):
        binding.run({})

    assert client.called is False


def test_mcp_host_tool_binding_rejects_readonly_session_mode_before_handler() -> None:
    binding, state = _make_mcp_host_binding(session_mode="readonly")

    with pytest.raises(RuntimeError, match="Blocked in readonly mode: MCP tool"):
        binding.run({})

    assert state["called"] is False


def test_build_tools_readonly_excludes_trusted_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="readonly",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )

    assert "project_echo" not in tools


@pytest.mark.parametrize(
    "runtime_kind",
    [
        RuntimeKind.INTERACTIVE_CHAT,
        RuntimeKind.ONE_SHOT,
        RuntimeKind.FORGE_EXEC,
    ],
)
def test_build_tools_top_level_write_runtimes_expose_trusted_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: RuntimeKind,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=runtime_kind,
    )

    assert "project_echo" in tools


def test_build_tools_exposes_custom_tool_capability_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(
        tmp_path,
        extra_manifest_lines=[
            '"capabilities": {',
            '    "read_only": True,',
            '    "network_access": "none",',
            '    "filesystem": {"read": "workspace", "write": "none"},',
            "},",
            '"output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},',
        ],
    )
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )

    metadata = tools["project_echo"].metadata
    assert metadata["tool_type"] == "custom_tool"
    custom_tool = metadata["custom_tool"]
    assert custom_tool["manifest_version"] == 1
    assert custom_tool["has_output_schema"] is True
    assert custom_tool["capabilities"]["read_only"] is True
    assert custom_tool["capabilities"]["filesystem_read_scope"] == "workspace"
    event_metadata = _tool_event_metadata(tools["project_echo"])
    assert event_metadata["tool_type"] == "custom_tool"
    assert event_metadata["custom_tool"]["capabilities"]["read_only"] is True
    assert "output_schema" not in event_metadata["custom_tool"]


def test_model_facing_custom_and_mcp_tool_schemas_strip_descriptive_bloat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(
        tmp_path,
        extra_manifest_lines=[
            '"input_schema": {',
            '    "type": "object",',
            '    "description": "CUSTOM_ROOT_DESCRIPTION",',
            '    "properties": {',
            '        "mode": {',
            '            "type": "string",',
            '            "description": "CUSTOM_MODE_DESCRIPTION",',
            '            "markdownDescription": "CUSTOM_MARKDOWN_DESCRIPTION",',
            '            "title": "CUSTOM_TITLE",',
            '            "examples": ["CUSTOM_EXAMPLE"],',
            '            "default": "CUSTOM_DEFAULT",',
            '            "enum": ["fast", "safe"],',
            "        },",
            "    },",
            '    "required": ["mode"],',
            "},",
        ],
    )
    _trust_project_custom_tool(tmp_path)
    binding, _client = _make_mcp_binding()
    binding = McpToolBinding(
        server_id=binding.server_id,
        tool_name=binding.tool_name,
        tool_alias=binding.tool_alias,
        description="MCP echo " + ("detail " * 500),
        parameters={
            "type": "object",
            "description": "MCP_ROOT_DESCRIPTION",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "MCP_TEXT_DESCRIPTION",
                    "default": "MCP_DEFAULT",
                }
            },
            "required": ["text"],
        },
        client=_client,
    )

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        mcp_manager=_DummyMcpManager(binding),
    )

    custom_tool = tools["project_echo"]
    custom_payload = custom_tool.as_openai_tool()  # type: ignore[attr-defined]
    custom_schema = custom_payload["function"]["parameters"]
    custom_serialized = json.dumps(custom_schema, ensure_ascii=True, sort_keys=True)
    assert "CUSTOM_ROOT_DESCRIPTION" not in custom_serialized
    assert "CUSTOM_MODE_DESCRIPTION" not in custom_serialized
    assert "CUSTOM_MARKDOWN_DESCRIPTION" not in custom_serialized
    assert "CUSTOM_TITLE" not in custom_serialized
    assert "CUSTOM_EXAMPLE" not in custom_serialized
    assert "CUSTOM_DEFAULT" not in custom_serialized
    assert custom_schema["properties"]["mode"]["enum"] == ["fast", "safe"]
    assert "CUSTOM_MODE_DESCRIPTION" in json.dumps(
        custom_tool.parameters,
        ensure_ascii=True,
        sort_keys=True,  # type: ignore[attr-defined]
    )
    assert custom_payload["function"]["description"] == "Project custom tool"

    mcp_tool = tools["mcp__alpha__echo"]
    mcp_payload = mcp_tool.as_openai_tool()  # type: ignore[attr-defined]
    mcp_schema = mcp_payload["function"]["parameters"]
    mcp_serialized = json.dumps(mcp_schema, ensure_ascii=True, sort_keys=True)
    assert "MCP_ROOT_DESCRIPTION" not in mcp_serialized
    assert "MCP_TEXT_DESCRIPTION" not in mcp_serialized
    assert "MCP_DEFAULT" not in mcp_serialized
    assert "MCP_TEXT_DESCRIPTION" in json.dumps(
        mcp_tool.parameters,
        ensure_ascii=True,
        sort_keys=True,  # type: ignore[attr-defined]
    )
    assert 800 < len(mcp_payload["function"]["description"]) <= 1000
    assert _tool_event_metadata(mcp_tool)["tool_type"] == "mcp"  # type: ignore[arg-type]


def test_model_facing_descriptions_honor_declared_family_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)
    binding, _client = _make_mcp_binding()
    binding = McpToolBinding(
        server_id=binding.server_id,
        tool_name=binding.tool_name,
        tool_alias=binding.tool_alias,
        description="MCP echo " + ("detail " * 500),
        parameters=binding.parameters,
        client=_client,
    )

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        mcp_manager=_DummyMcpManager(binding),
    )

    custom_tool = tools["project_echo"]
    assert custom_tool.metadata["model_description_max_chars"] == 1200  # type: ignore[attr-defined]
    # Manifest validation caps TOOL.description below 1200, so exercise the
    # declared limit on the built tool with a longer description swapped in.
    long_custom_tool = replace(
        custom_tool,  # type: ignore[type-var]
        description="Custom echo " + ("detail " * 500),
    )
    custom_payload = long_custom_tool.as_openai_tool()  # type: ignore[attr-defined]
    assert 1000 < len(custom_payload["function"]["description"]) <= 1200

    mcp_tool = tools["mcp__alpha__echo"]
    assert mcp_tool.metadata["model_description_max_chars"] == 1000  # type: ignore[attr-defined]
    mcp_payload = mcp_tool.as_openai_tool()  # type: ignore[attr-defined]
    assert 800 < len(mcp_payload["function"]["description"]) <= 1000


def test_build_tools_scope_restricted_session_excludes_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.FORGE_EXEC,
        allow_write_globs=["allowed.txt"],
    )

    assert "project_echo" not in tools


def test_build_tools_duplicate_default_protected_prefix_does_not_hide_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.FORGE_EXEC,
        deny_write_prefixes=[".alysis"],
    )

    assert "project_echo" in tools


def test_build_tools_extra_denied_prefixes_exclude_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        deny_write_prefixes=["blocked"],
    )

    assert "project_echo" not in tools


@pytest.mark.parametrize(
    "runtime_kind",
    [
        RuntimeKind.SWARM_WORKER,
        RuntimeKind.SUBAGENT,
        RuntimeKind.CONFLICT_AUTO_RESOLVE,
    ],
)
def test_build_tools_non_top_level_runtimes_exclude_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: RuntimeKind,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=runtime_kind,
    )

    assert "project_echo" not in tools


def test_review_mode_custom_tool_execution_requires_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)
    surface = _RecordingSurface()

    tools = _build_tools(
        tmp_path,
        mode="review",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        non_interactive=False,
        surface=surface,
    )

    result = tools["project_echo"].run({})

    assert result["success"] is True
    assert [request.kind for request in surface.requests] == ["custom_tool_run:project_echo"]
    assert (
        surface.requests[0].metadata["custom_tool"]["capabilities"]["network_access"]
        == "unspecified"
    )
    assert "capabilities:" in surface.requests[0].preview


@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_trusted_custom_tools_do_not_require_per_call_approval_outside_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)
    surface = _RecordingSurface()

    tools = _build_tools(
        tmp_path,
        mode=mode,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        non_interactive=False,
        surface=surface,
    )

    result = tools["project_echo"].run({})

    assert result["success"] is True
    assert surface.requests == []


def test_build_tools_custom_tools_disabled_by_config_excludes_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        cfg=AppConfig(
            model="test-model",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
            custom_tools_enabled=False,
        ),
    )

    assert "project_echo" not in tools
