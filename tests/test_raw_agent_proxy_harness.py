from __future__ import annotations

import os
from pathlib import Path

from scripts.qa.raw_agent_proxy import (
    RawAgentProxyScenario,
    _raw_agent_command,
    _raw_agent_env,
    _select_scenarios,
    extract_raw_agent_proxy_metrics,
    render_raw_agent_proxy_report,
)


def test_raw_agent_proxy_command_enables_benchmark_profile(tmp_path: Path) -> None:
    scenario = RawAgentProxyScenario(
        name="focused",
        instruction="Fix the project.",
        setup=lambda _path: None,
        verify_cmd=("python -m pytest -q",),
    )

    command = _raw_agent_command(
        scenario=scenario,
        repo_path=tmp_path,
        cli_command=("alysis",),
        base_url="http://127.0.0.1:9999/v1",
        use_mock_provider=True,
    )

    assert command[:2] == ("alysis", "run")
    assert "--benchmark" in command
    assert ("--path", os.fspath(tmp_path)) == (
        command[command.index("--path")],
        command[command.index("--path") + 1],
    )
    assert ("--base-url", "http://127.0.0.1:9999/v1") == (
        command[command.index("--base-url")],
        command[command.index("--base-url") + 1],
    )
    assert ("--verify-cmd", "python -m pytest -q") == (
        command[command.index("--verify-cmd")],
        command[command.index("--verify-cmd") + 1],
    )
    assert command[-1] == "Fix the project."


def test_raw_agent_proxy_real_provider_command_uses_caller_config(tmp_path: Path) -> None:
    scenario = RawAgentProxyScenario(
        name="focused",
        instruction="Fix the project.",
        setup=lambda _path: None,
    )

    command = _raw_agent_command(
        scenario=scenario,
        repo_path=tmp_path,
        cli_command=("alysis",),
        base_url=None,
        use_mock_provider=False,
    )

    assert "--benchmark" in command
    assert "--model" not in command
    assert "--api-key" not in command
    assert "--base-url" not in command


def test_raw_agent_proxy_env_sets_isolated_raw_profile(tmp_path: Path) -> None:
    env = _raw_agent_env(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")

    assert env["ALYSIS_CONFIG_DIR"] == os.fspath(tmp_path / "cfg")
    assert env["ALYSIS_DATA_DIR"] == os.fspath(tmp_path / "data")
    assert env["ALYSIS_RUN_PROFILE"] == "raw-benchmark"
    assert env["ALYSIS_API_KEY"] == "mock-key"
    assert "PYTHONPATH" in env


def test_raw_agent_proxy_real_provider_env_preserves_credentials(tmp_path: Path) -> None:
    env = _raw_agent_env(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        use_mock_provider=False,
    )

    assert env["ALYSIS_RUN_PROFILE"] == "raw-benchmark"
    assert "ALYSIS_CONFIG_DIR" not in env
    assert "ALYSIS_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_raw_agent_proxy_metrics_extract_controller_signals() -> None:
    events = [
        {"type": "tool_result", "payload": {"name": "fs_edit", "result": {"ok": True}}},
        {
            "type": "tool_result",
            "payload": {
                "name": "verify_run",
                "result": {"ok": False, "failure_summary": {"framework": "pytest"}},
            },
        },
        {"type": "tool_result", "payload": {"name": "test_discover", "result": {}}},
        {"type": "completion_gate_nudge", "payload": {}},
        {"type": "failed_verification_repair_attempt", "payload": {}},
        {"type": "tool_output_offloaded", "payload": {}},
        {"type": "final", "payload": {}},
    ]

    metrics = extract_raw_agent_proxy_metrics(
        events=events,
        exit_code=1,
        timed_out=False,
        command=("alysis", "run", "--benchmark", "task"),
    )

    assert metrics["benchmark_profile"] is True
    assert metrics["tool_call_count"] == 3
    assert metrics["material_tool_count"] == 1
    assert metrics["verify_run_count"] == 1
    assert metrics["verify_failure_count"] == 1
    assert metrics["failure_summary_count"] == 1
    assert metrics["test_discover_count"] == 1
    assert metrics["git_diff_count"] == 0
    assert metrics["completion_gate_nudge_count"] == 1
    assert metrics["completion_gate_recommended_actions"] == []
    assert metrics["completion_gate_forced_tool_choice_count"] == 0
    assert metrics["failed_verification_repair_count"] == 1
    assert metrics["tool_output_offload_count"] == 1
    assert metrics["final_count"] == 1


def test_raw_agent_proxy_scenario_selector_dedupes_all() -> None:
    scenarios = _select_scenarios(["write_file_smoke", "all", "write_file_smoke"])

    names = [scenario.name for scenario in scenarios]
    assert names.count("write_file_smoke") == 1
    assert "pytest_contract_smoke" in names
    assert "completion_gate_forced_verify" in names
    assert "completion_gate_forced_diff" in names


def test_raw_agent_proxy_report_includes_key_rates() -> None:
    class _Run:
        name = "example"
        exit_code = 0
        timed_out = False
        metrics = {
            "tool_call_count": 2,
            "verify_run_count": 1,
            "git_diff_count": 0,
            "completion_gate_nudge_count": 0,
            "passed": True,
            "oracle_message": "ok",
        }

    report = render_raw_agent_proxy_report(
        runs=(_Run(),),  # type: ignore[arg-type]
        metrics={
            "scenario_count": 1,
            "pass_rate": 1.0,
            "timeout_count": 0,
            "completion_gate_nudge_count": 0,
            "verify_run_count": 1,
            "test_discover_count": 0,
            "git_diff_count": 0,
            "oracle_failure_count": 0,
        },
    )

    assert "Raw Agent Proxy Harness" in report
    assert "pass_rate: 1.000" in report
    assert "| example | true | 0 | false | 2 | 1 | 0 | 0 | ok |" in report
