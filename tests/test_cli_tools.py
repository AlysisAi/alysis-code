from __future__ import annotations

import os

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig
from alysis_code.tools.availability import (
    _reset_tool_availability_for_tests,
    mark_unavailable,
    register_tool_availability,
)


@pytest.fixture(autouse=True)
def _clear_generic_web_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_API_KEY", raising=False)


def _row_map(cfg: AppConfig) -> dict[str, cli_mod._ToolAvailabilityRow]:
    return {row.name: row for row in cli_mod._tool_availability_rows(cfg)}


def test_tools_command_lists_core_built_in_tools(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "load_config", lambda: AppConfig())

    result = CliRunner().invoke(alysis_app, ["tools"])

    assert result.exit_code == 0
    assert "alysis tools" in result.output
    assert "web_fetch" in result.output
    assert "symbol_search" in result.output
    assert "fs_mkdir" in result.output
    assert "verify_run" in result.output
    assert "subagent_run" in result.output


def test_tool_rows_show_web_search_disabled_when_config_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rows = _row_map(AppConfig(web_search_mode="off"))

    assert rows["web_search"].status == "disabled"
    assert "mode=off" in rows["web_search"].notes
    assert "disabled by policy" in rows["web_search"].notes


def test_tool_rows_show_optional_unavailable_reason() -> None:
    _reset_tool_availability_for_tests()
    try:
        reason = "module not importable: fake_optional_dependency"
        register_tool_availability("knowledge_capture_json", optional=True)
        mark_unavailable("knowledge_capture_json", reason)

        rows = _row_map(AppConfig())

        assert rows["knowledge_capture_json"].status == "optional-unavailable"
        assert f"reason={reason}" in rows["knowledge_capture_json"].notes
    finally:
        _reset_tool_availability_for_tests()


def test_tool_rows_show_web_search_auto_unavailable_when_runtime_is_not_ready(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    rows = _row_map(
        AppConfig(
            model="gpt-5-mini",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        )
    )

    assert rows["web_search"].status == "auto-unavailable"
    assert "mode=auto" in rows["web_search"].notes
    assert "provider=(none)" in rows["web_search"].notes
    assert "OpenAI auto readiness requires explicit web_search_base_url" in rows["web_search"].notes
    assert "missing TAVILY_API_KEY" in rows["web_search"].notes
    assert "setup:" in rows["web_search"].notes
    assert "provider-agnostic fallback" in rows["web_search"].notes


def test_doctor_table_surfaces_web_search_setup_hint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")

    table = cli_mod._doctor_table(
        AppConfig(
            model="gpt-5-mini",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        )
    )
    console = Console(record=True, width=140)
    console.print(table)
    rendered = console.export_text()

    assert "web_search" in rendered
    assert "auto-unavailable" in rendered
    assert "web_search_provider" in rendered
    assert "web_search_setup" in rendered
    assert "TAVILY_API_KEY" in rendered


def test_tool_rows_show_web_search_available_when_auto_mode_is_ready(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "main-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    rows = _row_map(
        AppConfig(
            model="gpt-5-mini",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
        )
    )

    assert rows["web_search"].status == "available"
    assert "mode=auto" in rows["web_search"].notes
    assert "provider=openai_responses" in rows["web_search"].notes
    assert "ready for registration in main agent sessions" in rows["web_search"].notes
    assert "OpenAI Responses readiness is conservative" in rows["web_search"].notes


def test_tool_rows_show_ready_backend_disabled_by_search_policy(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "main-key")
    rows = _row_map(
        AppConfig(
            model="gpt-5-mini",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
            web_search_policy="off",
        )
    )

    assert rows["web_search"].status == "policy-disabled"
    assert "policy=off" in rows["web_search"].notes
    assert "prevents registration" in rows["web_search"].notes
    assert "ready for registration in main agent sessions" not in rows["web_search"].notes


def test_tool_rows_show_web_search_available_via_dashscope_chat(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "main-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    rows = _row_map(
        AppConfig(
            model="qwen3.5-plus",
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            web_search_mode="auto",
        )
    )

    assert rows["web_search"].status == "available"
    assert "mode=auto" in rows["web_search"].notes
    assert "provider=dashscope_chat" in rows["web_search"].notes
    assert "ready for registration in main agent sessions" in rows["web_search"].notes
    assert "available via DashScope Chat Completions enable_search" in rows["web_search"].notes


def test_tool_rows_show_web_search_available_via_native_chinese_provider_adapter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "main-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    rows = _row_map(
        AppConfig(
            model="kimi-k2.6",
            base_url="https://api.moonshot.cn/v1",
            web_search_mode="auto",
        )
    )

    assert rows["web_search"].status == "available"
    assert "mode=auto" in rows["web_search"].notes
    assert "provider=moonshot_kimi" in rows["web_search"].notes
    assert "ready for registration in main agent sessions" in rows["web_search"].notes
    assert "available via moonshot_kimi provider adapter" in rows["web_search"].notes


def test_tool_rows_show_web_search_available_via_tavily_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    rows = _row_map(
        AppConfig(
            model="gpt-5-mini",
            base_url="https://example-proxy.invalid/v1",
            web_search_mode="auto",
        )
    )

    assert rows["web_search"].status == "available"
    assert "mode=auto" in rows["web_search"].notes
    assert "provider=tavily" in rows["web_search"].notes
    assert "ready for registration in main agent sessions" in rows["web_search"].notes
    assert "available via model-independent Tavily adapter" in rows["web_search"].notes


def test_tool_rows_treat_legacy_on_mode_as_auto_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    rows = _row_map(
        AppConfig(
            base_url="",
            web_search_mode="on",
        )
    )

    assert rows["web_search"].status == "auto-unavailable"
    assert "mode=auto" in rows["web_search"].notes
    assert "provider=(none)" in rows["web_search"].notes
    assert "missing OpenAI search base URL" in rows["web_search"].notes
    assert "missing model" in rows["web_search"].notes
    assert "missing API key" in rows["web_search"].notes
    assert "missing TAVILY_API_KEY" in rows["web_search"].notes


def test_tool_rows_only_mark_hidden_tools_as_hidden_from_built_in_readonly_subagents() -> None:
    rows = _row_map(AppConfig())

    assert rows["web_search"].notes.endswith("hidden from built-in readonly subagents")
    assert rows["web_fetch"].notes.endswith("hidden from built-in readonly subagents")
    assert rows["fs_read"].notes == "-"
    assert rows["history_search"].notes == "-"


def test_tools_command_output_is_concise_and_user_readable(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod,
        "load_config",
        lambda: AppConfig(
            model="gpt-5-mini",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
        ),
    )
    monkeypatch.setenv("ALYSIS_API_KEY", "main-key")

    result = CliRunner().invoke(alysis_app, ["tools"])

    assert result.exit_code == 0
    assert "`alysis tools` shows the built-in catalog" in result.output
    assert "config-dependent" in result.output
    assert "availability" in result.output
    assert "`web_search` discovers candidate sources" in result.output
    assert "`web_fetch` retrieves a specific" in result.output
    assert "chosen URL" in result.output
    assert "Top-level readonly/Plan sessions can use ready web tools" in result.output
    assert "Custom tools are managed separately via `alysis tool" in result.output
    assert "trust|untrust`" in result.output
    assert "web_search_mode=off|auto|native|external" in result.output
    assert "OpenAI Responses" in result.output
    assert "DashScope Chat" in result.output
    assert "Tavily" in result.output
    assert "TAVILY_API_KEY" in result.output
    assert "native` never uses Tavily" in result.output
    assert "external` uses only external" in result.output
    assert "Legacy `on`" in result.output
    assert "`web_search_enabled` values still load as `auto`" in result.output
    assert "browser support" not in result.output.lower()
    assert "exa support" not in result.output.lower()
    assert "registration_ready" not in result.output
    assert "api_key_available" not in result.output
    assert "{" not in result.output
