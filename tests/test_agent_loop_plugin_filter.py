from __future__ import annotations

from collections import Counter
from pathlib import Path

from alysis_code import agent_loop
from alysis_code.custom_tools import (
    CustomToolCapabilities,
    CustomToolDiscoveryResult,
    CustomToolSessionState,
    CustomToolSpec,
)
from alysis_code.custom_tools.session import CustomToolCatalogEntry
from alysis_code.custom_tools.trust import ProjectToolTrustState
from alysis_code.extensions.activation import ActivationDecision
from alysis_code.mcp.models import ResolvedMcpConfig, ResolvedMcpServer
from alysis_code.runtime_kind import RuntimeKind


def _decision(*, enabled: tuple[str, ...]) -> ActivationDecision:
    return ActivationDecision(
        enabled_plugin_ids=frozenset(enabled),
        workspace_trust_was_prompted=False,
        workspace_trust_granted=False,
        untrusted_project_plugin_ids=frozenset(),
    )


def _index() -> agent_loop._PluginActivationIndex:
    return agent_loop._PluginActivationIndex(
        slug_to_plugin_id={"acme-demo": "acme.demo"},
        skill_lookup_to_plugin_id={},
    )


def _tool(name: str, relative_path: str) -> CustomToolSpec:
    return CustomToolSpec(
        name=name,
        description="Tool",
        input_schema={"type": "object", "properties": {}},
        manifest_version=1,
        capabilities=CustomToolCapabilities(),
        output_schema=None,
        timeout_s=1.0,
        required_env=(),
        enabled_in=(RuntimeKind.INTERACTIVE_CHAT.value,),
        isolation="subprocess",
        source_scope="global",
        source_path=Path("/tmp") / relative_path,
        relative_tool_path=relative_path,
        file_hash="f" * 64,
        missing_env=(),
    )


def _tool_state(*tools: CustomToolSpec) -> CustomToolSessionState:
    discovery = CustomToolDiscoveryResult(
        global_tools=tools,
        project_tools=(),
        effective_tools=tools,
        shadowed_tools=(),
        issues=(),
    )
    return CustomToolSessionState(
        discovery=discovery,
        trust_state=ProjectToolTrustState(),
        catalog_entries=tuple(
            CustomToolCatalogEntry(
                name=tool.name,
                source_scope=tool.source_scope,
                trust="global",
                status="available",
                source_path=tool.source_path,
                spec=tool,
                exposed=True,
            )
            for tool in tools
        ),
        effective_tools_by_name=discovery.effective_tools_by_name(),
        exposed_tools_by_name=discovery.effective_tools_by_name(),
    )


def test_disabled_plugin_tool_absent_from_session_toolset() -> None:
    tool = _tool("plugin_tool", "plugins/acme-demo/tool.py")

    filtered, dropped = agent_loop._filter_custom_tool_session_state_for_plugins(
        state=_tool_state(tool),
        activation_decision=_decision(enabled=()),
        index=_index(),
    )

    assert filtered.exposed_tools_by_name == {}
    assert dropped == {"acme.demo": 1}


def test_enabled_plugin_tool_present_in_session_toolset() -> None:
    tool = _tool("plugin_tool", "plugins/acme-demo/tool.py")

    filtered, dropped = agent_loop._filter_custom_tool_session_state_for_plugins(
        state=_tool_state(tool),
        activation_decision=_decision(enabled=("acme.demo",)),
        index=_index(),
    )

    assert "plugin_tool" in {tool.name for tool in filtered.discovery.effective_tools}
    assert dropped == {}


def test_builtin_tool_without_plugin_marker_is_always_retained() -> None:
    tool = _tool("local_tool", "local_tool.py")

    filtered, dropped = agent_loop._filter_custom_tool_session_state_for_plugins(
        state=_tool_state(tool),
        activation_decision=_decision(enabled=()),
        index=_index(),
    )

    assert "local_tool" in {tool.name for tool in filtered.discovery.effective_tools}
    assert dropped == {}


def test_mcp_server_from_disabled_plugin_is_filtered() -> None:
    config = ResolvedMcpConfig(
        workspace_root=Path("/tmp/repo"),
        user_config_path=Path("/tmp/mcp.json"),
        project_config_path=Path("/tmp/repo/.alysis/mcp.json"),
        user_config_present=True,
        project_config_present=False,
        servers=(
            ResolvedMcpServer(
                id="acme.demo/demo_server",
                transport="stdio",
                enabled=True,
                enabled_in=None,
                trust="explicit",
                allowed_tools=(),
                denied_tools=(),
                startup_timeout_s=1.0,
                call_timeout_s=1.0,
                tool_prefix=None,
                command="python",
            ),
        ),
    )

    filtered, dropped = agent_loop._filter_mcp_config_for_plugins(
        config=config,
        activation_decision=_decision(enabled=()),
    )

    assert filtered.servers == ()
    assert dropped == {"acme.demo": 1}


def test_workspace_untrusted_project_plugin_not_active_but_user_plugin_active() -> None:
    activation = ActivationDecision(
        enabled_plugin_ids=frozenset({"acme.user"}),
        workspace_trust_was_prompted=False,
        workspace_trust_granted=False,
        untrusted_project_plugin_ids=frozenset({"acme.demo"}),
    )

    assert agent_loop._component_plugin_allowed("acme.user", activation, Counter())
    dropped: Counter[str] = Counter()
    assert not agent_loop._component_plugin_allowed("acme.demo", activation, dropped)
    assert dropped == {"acme.demo": 1}
