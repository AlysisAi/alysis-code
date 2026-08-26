from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.mcp.oauth_store import (
    McpOAuthTokenRecord,
    load_oauth_token_record,
    save_oauth_token_record,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_http_oauth_config(
    tmp_path: Path,
    *,
    servers: dict[str, dict[str, object]],
) -> dict[str, str]:
    cfg_dir = tmp_path / "cfg"
    _write_json(cfg_dir / "mcp.json", {"servers": servers})
    return {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}


def _token_record(*, access_token: str, refresh_token: str | None) -> McpOAuthTokenRecord:
    obtained_at = datetime.now(UTC).replace(microsecond=0)
    return McpOAuthTokenRecord(
        access_token=access_token,
        token_type="Bearer",
        expires_at=obtained_at + timedelta(hours=1),
        refresh_token=refresh_token,
        granted_scopes=("openid", "profile"),
        obtained_at=obtained_at,
    )


def test_mcp_auth_login_cli_completes_with_localhost_callback(
    tmp_path: Path, monkeypatch, oauth_fixture_server
) -> None:
    oauth_fixture_server.expected_authorize_resource = oauth_fixture_server.protected_url
    oauth_fixture_server.expected_token_resource = oauth_fixture_server.protected_url
    env = _write_http_oauth_config(
        tmp_path,
        servers={
            "alpha": {
                "transport": "http",
                "url": oauth_fixture_server.protected_url,
                "oauth": {
                    "client_id": "test-client",
                    "scopes": ["openid", "profile"],
                },
            }
        },
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", env["ALYSIS_CONFIG_DIR"])

    def _browser_open(url: str) -> bool:
        response = httpx.get(url, follow_redirects=True, timeout=5.0)
        assert response.status_code == 200
        return True

    monkeypatch.setattr("alysis_code.mcp.oauth_runtime.webbrowser.open", _browser_open)

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "auth", "login", "alpha", "--path", str(tmp_path)],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert "OAuth login succeeded for MCP server 'alpha'." in result.output
    assert "Granted scopes: openid, profile" in result.output
    assert "test_access" not in result.output
    assert "test_refresh" not in result.output
    assert (
        oauth_fixture_server.authorize_requests[-1]["resource"]
        == oauth_fixture_server.protected_url
    )
    assert oauth_fixture_server.token_requests[-1]["resource"] == oauth_fixture_server.protected_url
    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "test_access"
    assert stored.refresh_token == "test_refresh"


def test_mcp_auth_login_cli_falls_back_to_metadata_scopes_supported(
    tmp_path: Path, monkeypatch, oauth_fixture_server
) -> None:
    oauth_fixture_server.expected_authorize_resource = oauth_fixture_server.protected_url
    oauth_fixture_server.expected_token_resource = oauth_fixture_server.protected_url
    oauth_fixture_server.protected_resource_payload_override = {
        "resource": oauth_fixture_server.protected_url,
        "authorization_servers": [oauth_fixture_server.authorization_server_url],
        "scopes_supported": ["openid", " profile ", "email", "openid"],
    }
    env = _write_http_oauth_config(
        tmp_path,
        servers={
            "alpha": {
                "transport": "http",
                "url": oauth_fixture_server.protected_url,
                "oauth": {
                    "client_id": "test-client",
                },
            }
        },
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", env["ALYSIS_CONFIG_DIR"])

    def _browser_open(url: str) -> bool:
        response = httpx.get(url, follow_redirects=True, timeout=5.0)
        assert response.status_code == 200
        return True

    monkeypatch.setattr("alysis_code.mcp.oauth_runtime.webbrowser.open", _browser_open)

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "auth", "login", "alpha", "--path", str(tmp_path)],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert oauth_fixture_server.authorize_requests[-1]["scope"] == "openid profile email"
    assert (
        oauth_fixture_server.authorize_requests[-1]["resource"]
        == oauth_fixture_server.protected_url
    )
    assert oauth_fixture_server.token_requests[-1]["resource"] == oauth_fixture_server.protected_url
    assert "Granted scopes: openid, profile, email" in result.output
    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.granted_scopes == ("openid", "profile", "email")


def test_mcp_auth_login_cli_requires_protected_resource_metadata_for_scope_fallback(
    tmp_path: Path, monkeypatch, oauth_fixture_server
) -> None:
    oauth_fixture_server.serve_path_protected_resource_metadata = False
    oauth_fixture_server.serve_root_protected_resource_metadata = False
    env = _write_http_oauth_config(
        tmp_path,
        servers={
            "alpha": {
                "transport": "http",
                "url": oauth_fixture_server.protected_url,
                "oauth": {
                    "client_id": "test-client",
                    "authorization_server_url": oauth_fixture_server.authorization_server_url,
                },
            }
        },
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", env["ALYSIS_CONFIG_DIR"])

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "auth", "login", "alpha", "--path", str(tmp_path)],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 1
    assert "protected resource metadata discovery failed" in " ".join(result.output.split())
    assert not oauth_fixture_server.authorize_requests
    assert not oauth_fixture_server.token_requests
    assert load_oauth_token_record("alpha") is None


def test_mcp_auth_status_cli_lists_oauth_servers_and_redacts_tokens(
    tmp_path: Path, monkeypatch
) -> None:
    env = _write_http_oauth_config(
        tmp_path,
        servers={
            "alpha": {
                "transport": "http",
                "url": "https://example.com/mcp",
                "oauth": {"client_id": "alpha-client"},
            },
            "beta": {
                "transport": "http",
                "url": "https://example.com/other",
                "oauth": {"client_id": "beta-client"},
            },
            "gamma": {
                "transport": "stdio",
                "command": "echo",
                "args": ["hi"],
            },
        },
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", env["ALYSIS_CONFIG_DIR"])
    save_oauth_token_record(
        "alpha", _token_record(access_token="alpha-secret-token", refresh_token="alpha-refresh")
    )

    result = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "auth", "status", "--path", str(tmp_path)],
        env=env,
        terminal_width=200,
    )
    targeted = CliRunner().invoke(
        cli_mod.app,
        ["mcp", "auth", "status", "--path", str(tmp_path), "--server", "alpha"],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert targeted.exit_code == 0
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "gamma" not in result.output
    assert "present" in result.output
    assert "absent" in result.output
    assert "openid, profile" in result.output
    assert "alpha-secret-token" not in result.output
    assert "alpha-refresh" not in result.output
    assert "alpha" in targeted.output
    assert "beta" not in targeted.output


def test_mcp_auth_logout_cli_deletes_stored_tokens_cleanly(tmp_path: Path, monkeypatch) -> None:
    env = _write_http_oauth_config(
        tmp_path,
        servers={
            "alpha": {
                "transport": "http",
                "url": "https://example.com/mcp",
                "oauth": {"client_id": "alpha-client"},
            }
        },
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", env["ALYSIS_CONFIG_DIR"])
    save_oauth_token_record(
        "alpha", _token_record(access_token="logout-secret", refresh_token="logout-refresh")
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["mcp", "auth", "logout", "alpha", "--path", str(tmp_path)],
        env=env,
        terminal_width=200,
    )
    repeat = runner.invoke(
        cli_mod.app,
        ["mcp", "auth", "logout", "alpha", "--path", str(tmp_path)],
        env=env,
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert repeat.exit_code == 0
    assert "Cleared stored OAuth tokens for MCP server 'alpha'." in result.output
    assert "No stored OAuth tokens found for MCP server 'alpha'." in repeat.output
    assert load_oauth_token_record("alpha") is None
