from __future__ import annotations

from alysis_code.mcp.forge_scope import (
    describe_task_mcp_scope,
    normalize_task_mcp_scope,
    serialize_task_mcp_scope,
)


def test_normalize_task_mcp_scope_dedupes_stably() -> None:
    scope, warnings = normalize_task_mcp_scope(
        {
            "allow_resources": True,
            "allowed_tools": [
                {"server_id": "github", "tool_name": "create_issue"},
                {"server_id": "github", "tool_name": "create_issue"},
                {"server_id": "github", "tool_name": "comment_pull_request"},
            ],
        },
        warning_prefix="Task T01",
    )

    assert warnings == []
    assert scope is not None
    assert scope.allow_resources is True
    assert [item.as_payload() for item in scope.allowed_tools] == [
        {"server_id": "github", "tool_name": "create_issue"},
        {"server_id": "github", "tool_name": "comment_pull_request"},
    ]
    assert serialize_task_mcp_scope(scope) == {
        "allow_resources": True,
        "allowed_tools": [
            {"server_id": "github", "tool_name": "create_issue"},
            {"server_id": "github", "tool_name": "comment_pull_request"},
        ],
    }


def test_normalize_task_mcp_scope_drops_invalid_entries_with_warnings() -> None:
    scope, warnings = normalize_task_mcp_scope(
        {
            "allow_resources": "yes",
            "allowed_tools": [
                {"server_id": "", "tool_name": "create_issue"},
                {"server_id": "github", "tool_name": ""},
                {"server_id": "github", "tool_name": "create_issue"},
            ],
        },
        warning_prefix="Task T01",
    )

    assert scope is not None
    assert scope.allow_resources is False
    assert [item.as_payload() for item in scope.allowed_tools] == [
        {"server_id": "github", "tool_name": "create_issue"}
    ]
    assert any("allow_resources" in warning for warning in warnings)
    assert any("server_id cannot be empty" in warning for warning in warnings)
    assert any("tool_name cannot be empty" in warning for warning in warnings)


def test_normalize_task_mcp_scope_empty_normalizes_to_deny_by_default() -> None:
    scope, warnings = normalize_task_mcp_scope(
        {},
        warning_prefix="Task T01",
    )

    assert scope is None
    assert warnings == [
        "Task T01: normalized empty mcp_scope; MCP execution remains deny-by-default."
    ]
    assert describe_task_mcp_scope(scope) == "disabled (default deny-by-default)"
