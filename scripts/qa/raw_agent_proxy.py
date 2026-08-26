from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    for _path in (os.fspath(_REPO_ROOT), os.fspath(_REPO_ROOT / "src")):
        if _path not in sys.path:
            sys.path.insert(0, _path)
    __package__ = "scripts.qa"

from alysis_code.session_store import read_session_events

from .mock_llm import MockLLMServer

QA_MODEL = "qa-mock-model"
DEFAULT_SCENARIO_TIMEOUT_S = 90.0
_MATERIAL_TOOL_NAMES = {
    "fs_write",
    "fs_edit",
    "git_apply_patch",
    "fs_move",
    "fs_copy",
    "fs_delete",
    "fs_mkdir",
    "shell_service_start",
}


@dataclass(frozen=True)
class RawAgentProxyScenario:
    name: str
    instruction: str
    setup: Callable[[Path], None]
    verify_cmd: tuple[str, ...] = ()
    oracle: Callable[[Path], tuple[bool, str]] | None = None


@dataclass(frozen=True)
class RawAgentProxyRun:
    name: str
    instruction: str
    command: tuple[str, ...]
    repo_path: Path
    work_dir: Path
    transcript_path: Path
    session_paths: tuple[Path, ...]
    exit_code: int | None
    timed_out: bool
    duration_s: float
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "instruction": self.instruction,
            "command": list(self.command),
            "repo_path": os.fspath(self.repo_path),
            "work_dir": os.fspath(self.work_dir),
            "transcript_path": os.fspath(self.transcript_path),
            "session_paths": [os.fspath(path) for path in self.session_paths],
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "metrics": self.metrics,
        }


@dataclass(frozen=True)
class RawAgentProxyHarnessResult:
    output_dir: Path
    runs_path: Path
    metrics_path: Path
    report_path: Path
    runs: tuple[RawAgentProxyRun, ...]
    metrics: dict[str, Any]


def run_raw_agent_proxy_harness(
    *,
    output_dir: Path,
    scenario_names: tuple[str, ...] | list[str] | None = None,
    scenario_timeout_s: float = DEFAULT_SCENARIO_TIMEOUT_S,
    cli_command: tuple[str, ...] | None = None,
    use_mock_provider: bool = True,
) -> RawAgentProxyHarnessResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios = _select_scenarios(scenario_names)
    started = time.monotonic()
    runs: list[RawAgentProxyRun] = []
    if use_mock_provider:
        with MockLLMServer() as server:
            for scenario in scenarios:
                runs.append(
                    drive_raw_agent_proxy_scenario(
                        scenario=scenario,
                        output_dir=output_dir,
                        timeout_s=scenario_timeout_s,
                        cli_command=cli_command,
                        base_url=server.base_url,
                        use_mock_provider=True,
                    )
                )
    else:
        for scenario in scenarios:
            runs.append(
                drive_raw_agent_proxy_scenario(
                    scenario=scenario,
                    output_dir=output_dir,
                    timeout_s=scenario_timeout_s,
                    cli_command=cli_command,
                    base_url=None,
                    use_mock_provider=False,
                )
            )
    aggregate_metrics = _aggregate_raw_agent_proxy_metrics(
        runs=tuple(runs),
        duration_s=time.monotonic() - started,
    )
    runs_path = output_dir / "raw_agent_proxy_runs.json"
    metrics_path = output_dir / "raw_agent_proxy_metrics.json"
    report_path = output_dir / "raw_agent_proxy_report.md"
    runs_path.write_text(
        json.dumps([run.to_dict() for run in runs], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metrics_path.write_text(
        json.dumps(aggregate_metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        render_raw_agent_proxy_report(runs=tuple(runs), metrics=aggregate_metrics),
        encoding="utf-8",
    )
    return RawAgentProxyHarnessResult(
        output_dir=output_dir,
        runs_path=runs_path,
        metrics_path=metrics_path,
        report_path=report_path,
        runs=tuple(runs),
        metrics=aggregate_metrics,
    )


def drive_raw_agent_proxy_scenario(
    *,
    scenario: RawAgentProxyScenario,
    output_dir: Path,
    timeout_s: float,
    cli_command: tuple[str, ...] | None = None,
    base_url: str | None = None,
    use_mock_provider: bool = True,
) -> RawAgentProxyRun:
    started = time.monotonic()
    work_dir = output_dir / "workdirs" / scenario.name
    repo_path = work_dir / "repo"
    config_dir = work_dir / "config"
    data_dir = work_dir / "data"
    transcript_path = output_dir / "transcripts" / f"{scenario.name}.log"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    scenario.setup(repo_path)
    if use_mock_provider:
        _write_raw_agent_config(config_dir=config_dir, base_url=base_url)
    command = _raw_agent_command(
        scenario=scenario,
        repo_path=repo_path,
        cli_command=cli_command,
        base_url=base_url,
        use_mock_provider=use_mock_provider,
    )
    env = _raw_agent_env(
        config_dir=config_dir,
        data_dir=data_dir,
        use_mock_provider=use_mock_provider,
    )
    completed = _run_cli_command(
        command=command,
        cwd=work_dir,
        env=env,
        timeout_s=timeout_s,
        transcript_path=transcript_path,
    )
    session_paths = _discover_session_paths(data_dir)
    events = _read_events(session_paths)
    metrics = extract_raw_agent_proxy_metrics(
        events=events,
        exit_code=completed.returncode,
        timed_out=completed.timed_out,
        command=command,
    )
    oracle_passed = True
    oracle_message = "no oracle configured"
    if scenario.oracle is not None:
        try:
            oracle_passed, oracle_message = scenario.oracle(repo_path)
        except Exception as exc:  # noqa: BLE001
            oracle_passed = False
            oracle_message = f"oracle error: {exc}"
    run_passed = bool(completed.returncode == 0 and not completed.timed_out and oracle_passed)
    metrics.update(
        {
            "oracle_passed": oracle_passed,
            "oracle_message": oracle_message,
            "passed": run_passed,
        }
    )
    return RawAgentProxyRun(
        name=scenario.name,
        instruction=scenario.instruction,
        command=command,
        repo_path=repo_path,
        work_dir=work_dir,
        transcript_path=transcript_path,
        session_paths=session_paths,
        exit_code=completed.returncode,
        timed_out=completed.timed_out,
        duration_s=time.monotonic() - started,
        metrics=metrics,
    )


def extract_raw_agent_proxy_metrics(
    *,
    events: list[dict[str, Any]],
    exit_code: int | None,
    timed_out: bool,
    command: tuple[str, ...],
) -> dict[str, Any]:
    tool_names: list[str] = []
    verify_failures = 0
    failure_summaries = 0
    for event in events:
        if event.get("type") != "tool_result":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        name = str(payload.get("name") or "")
        if name:
            tool_names.append(name)
        result = payload.get("result")
        if isinstance(result, dict):
            if name == "verify_run" and result.get("ok") is False:
                verify_failures += 1
            if isinstance(result.get("failure_summary"), dict):
                failure_summaries += 1
    event_types = [str(event.get("type") or "") for event in events]
    recommended_actions: list[str] = []
    forced_tool_choices = 0
    for event in events:
        if event.get("type") != "completion_gate_nudge":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        action = str(payload.get("completion_gate_recommended_action") or "").strip()
        if action:
            recommended_actions.append(action)
        if payload.get("forced_tool_choice"):
            forced_tool_choices += 1
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "benchmark_profile": "--benchmark" in command,
        "event_count": len(events),
        "tool_call_count": len(tool_names),
        "tool_names": tool_names,
        "material_tool_count": sum(1 for name in tool_names if name in _MATERIAL_TOOL_NAMES),
        "verify_run_count": tool_names.count("verify_run"),
        "verify_failure_count": verify_failures,
        "failure_summary_count": failure_summaries,
        "test_discover_count": tool_names.count("test_discover"),
        "git_diff_count": tool_names.count("git_diff"),
        "completion_gate_nudge_count": event_types.count("completion_gate_nudge"),
        "completion_gate_recommended_actions": recommended_actions,
        "completion_gate_forced_tool_choice_count": forced_tool_choices,
        "failed_verification_repair_count": event_types.count("failed_verification_repair_attempt"),
        "tool_output_offload_count": event_types.count("tool_output_offloaded"),
        "final_count": event_types.count("final"),
    }


def render_raw_agent_proxy_report(
    *,
    runs: tuple[RawAgentProxyRun, ...],
    metrics: dict[str, Any],
) -> str:
    lines = [
        "# Raw Agent Proxy Harness",
        "",
        f"- scenarios: {metrics['scenario_count']}",
        f"- pass_rate: {metrics['pass_rate']:.3f}",
        f"- timeout_count: {metrics['timeout_count']}",
        f"- completion_gate_nudge_count: {metrics['completion_gate_nudge_count']}",
        f"- verify_run_count: {metrics['verify_run_count']}",
        f"- test_discover_count: {metrics['test_discover_count']}",
        f"- git_diff_count: {metrics['git_diff_count']}",
        f"- oracle_failure_count: {metrics['oracle_failure_count']}",
        "",
        "| scenario | pass | exit | timeout | tools | verify | diff | gate nudges | oracle |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for run in runs:
        run_metrics = run.metrics
        lines.append(
            "| {name} | {passed} | {exit_code} | {timeout} | {tools} | {verify} | {diff} | {nudges} | {oracle} |".format(
                name=run.name,
                passed=str(bool(run_metrics.get("passed"))).lower(),
                exit_code=run.exit_code,
                timeout=str(run.timed_out).lower(),
                tools=run_metrics["tool_call_count"],
                verify=run_metrics["verify_run_count"],
                diff=run_metrics["git_diff_count"],
                nudges=run_metrics["completion_gate_nudge_count"],
                oracle=str(run_metrics.get("oracle_message") or "").replace("|", "\\|")[:80],
            )
        )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _CompletedCommand:
    returncode: int | None
    timed_out: bool


def _run_cli_command(
    *,
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    transcript_path: Path,
) -> _CompletedCommand:
    merged_env = os.environ.copy()
    merged_env.update(env)
    process = subprocess.Popen(
        command,
        cwd=os.fspath(cwd),
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        output, _ = process.communicate(timeout=5)
    transcript_path.write_text(output or "", encoding="utf-8")
    return _CompletedCommand(returncode=process.returncode, timed_out=timed_out)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()


def _raw_agent_command(
    *,
    scenario: RawAgentProxyScenario,
    repo_path: Path,
    cli_command: tuple[str, ...] | None,
    base_url: str | None,
    use_mock_provider: bool = True,
) -> tuple[str, ...]:
    command = cli_command or (
        sys.executable,
        "-m",
        "alysis_code.cli",
    )
    args = [
        *command,
        "run",
        "--benchmark",
        "--no-stream",
        "--path",
        os.fspath(repo_path),
    ]
    if use_mock_provider:
        args.extend(["--model", QA_MODEL, "--api-key", "qa-key"])
    if use_mock_provider and base_url:
        args.extend(["--base-url", base_url])
    for verify_cmd in scenario.verify_cmd:
        args.extend(["--verify-cmd", verify_cmd])
    args.append(scenario.instruction)
    return tuple(args)


def _raw_agent_env(
    *,
    config_dir: Path,
    data_dir: Path,
    use_mock_provider: bool = True,
) -> dict[str, str]:
    pythonpath = os.pathsep.join(
        path
        for path in (
            os.fspath(Path(__file__).resolve().parents[2] / "src"),
            os.fspath(Path(__file__).resolve().parents[2]),
            os.environ.get("PYTHONPATH", ""),
        )
        if path
    )
    env = {
        "NO_COLOR": "1",
        "PYTHONPATH": pythonpath,
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_DATA_DIR": os.fspath(data_dir),
        "ALYSIS_MAX_OUTPUT_TOKENS": "4096",
        "ALYSIS_RUN_PROFILE": "raw-benchmark",
        "ALYSIS_SHELL_SANDBOX_MODE": "off",
        "ALYSIS_VERIFY_SANDBOX_MODE": "off",
    }
    if use_mock_provider:
        env["OPENAI_API_KEY"] = "mock-key"
        env["ALYSIS_CONFIG_DIR"] = os.fspath(config_dir)
        env["ALYSIS_API_KEY"] = "mock-key"
    return env


def _write_raw_agent_config(*, config_dir: Path, base_url: str | None) -> None:
    if not base_url:
        return
    config = {
        "active_profile": "mock",
        "base_url": base_url,
        "model": QA_MODEL,
        "profiles": {
            "mock": {
                "name": "mock",
                "protocol": "openai_compat",
                "base_url": base_url,
                "api_key_env": "ALYSIS_API_KEY",
                "default_model": QA_MODEL,
                "extra_headers": {},
                "notes": "Raw agent proxy mock provider",
            }
        },
        "routing_mode": "code_only",
        "skills_enabled": False,
        "skills_auto_invoke": False,
        "custom_tools_enabled": False,
        "subagents_enabled": False,
        "web_search_mode": "off",
    }
    (config_dir / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _discover_session_paths(data_dir: Path) -> tuple[Path, ...]:
    sessions_dir = data_dir / "sessions"
    if not sessions_dir.exists():
        return ()
    return tuple(sorted(sessions_dir.glob("*.jsonl"), key=lambda path: path.name))


def _read_events(session_paths: tuple[Path, ...]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for session_path in session_paths:
        events.extend(read_session_events(session_path))
    return events


def _aggregate_raw_agent_proxy_metrics(
    *,
    runs: tuple[RawAgentProxyRun, ...],
    duration_s: float,
) -> dict[str, Any]:
    scenario_count = len(runs)
    passed = sum(
        1
        for run in runs
        if bool(run.metrics.get("passed", run.exit_code == 0 and not run.timed_out))
    )
    return {
        "scenario_count": scenario_count,
        "passed": passed,
        "failed": scenario_count - passed,
        "pass_rate": (passed / scenario_count) if scenario_count else 0.0,
        "timeout_count": sum(1 for run in runs if run.timed_out),
        "total_duration_s": round(duration_s, 3),
        "tool_call_count": sum(run.metrics["tool_call_count"] for run in runs),
        "material_tool_count": sum(run.metrics["material_tool_count"] for run in runs),
        "verify_run_count": sum(run.metrics["verify_run_count"] for run in runs),
        "verify_failure_count": sum(run.metrics["verify_failure_count"] for run in runs),
        "failure_summary_count": sum(run.metrics["failure_summary_count"] for run in runs),
        "test_discover_count": sum(run.metrics["test_discover_count"] for run in runs),
        "git_diff_count": sum(run.metrics["git_diff_count"] for run in runs),
        "completion_gate_nudge_count": sum(
            run.metrics["completion_gate_nudge_count"] for run in runs
        ),
        "completion_gate_forced_tool_choice_count": sum(
            run.metrics["completion_gate_forced_tool_choice_count"] for run in runs
        ),
        "oracle_failure_count": sum(1 for run in runs if run.metrics.get("oracle_passed") is False),
        "failed_verification_repair_count": sum(
            run.metrics["failed_verification_repair_count"] for run in runs
        ),
        "tool_output_offload_count": sum(run.metrics["tool_output_offload_count"] for run in runs),
    }


def _setup_basic_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "README.md").write_text("raw agent proxy fixture\n", encoding="utf-8")
    src_dir = repo_path / "src"
    src_dir.mkdir()
    (src_dir / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )


def _setup_pytest_repo(repo_path: Path) -> None:
    _setup_basic_repo(repo_path)
    tests_dir = repo_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )


def _setup_git_readme_repo(repo_path: Path) -> None:
    _setup_basic_repo(repo_path)
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Raw Agent QA"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "raw-agent-qa@example.invalid"],
        cwd=repo_path,
        check=True,
    )
    subprocess.run(["git", "add", "README.md", "src/calc.py"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo_path, check=True)


def _oracle_file_equals(rel_path: str, expected: str) -> Callable[[Path], tuple[bool, str]]:
    def check(repo_path: Path) -> tuple[bool, str]:
        path = repo_path / rel_path
        if not path.exists():
            return (False, f"{rel_path} missing")
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            return (False, f"{rel_path} content mismatch")
        return (True, f"{rel_path} matches expected content")

    return check


def _oracle_contains(rel_path: str, expected: str) -> Callable[[Path], tuple[bool, str]]:
    def check(repo_path: Path) -> tuple[bool, str]:
        path = repo_path / rel_path
        if not path.exists():
            return (False, f"{rel_path} missing")
        actual = path.read_text(encoding="utf-8")
        if expected not in actual:
            return (False, f"{rel_path} does not contain expected text")
        return (True, f"{rel_path} contains expected text")

    return check


RAW_AGENT_PROXY_SCENARIOS: dict[str, RawAgentProxyScenario] = {
    "write_file_smoke": RawAgentProxyScenario(
        name="write_file_smoke",
        instruction=(
            "Write file qa_written.txt with the content qa write. "
            "Run the configured verification when done."
        ),
        setup=_setup_basic_repo,
        verify_cmd=('test "$(cat qa_written.txt)" = "qa write"',),
        oracle=_oracle_file_equals("qa_written.txt", "qa write\n"),
    ),
    "pytest_contract_smoke": RawAgentProxyScenario(
        name="pytest_contract_smoke",
        instruction=(
            "Inspect the Python project and keep the add function correct. "
            "Run the configured verification when done."
        ),
        setup=_setup_pytest_repo,
        verify_cmd=("python -m pytest tests/test_calc.py -q",),
        oracle=_oracle_contains("src/calc.py", "return a + b"),
    ),
    "completion_gate_forced_verify": RawAgentProxyScenario(
        name="completion_gate_forced_verify",
        instruction=(
            "RAW_PROXY_FORCE_VERIFY: update src/calc.py with a harmless marker, then "
            "the controller should force configured verification before final."
        ),
        setup=_setup_basic_repo,
        verify_cmd=(f"{shlex.quote(sys.executable)} -m py_compile src/calc.py",),
        oracle=_oracle_contains("src/calc.py", "# forced verify marker"),
    ),
    "completion_gate_forced_diff": RawAgentProxyScenario(
        name="completion_gate_forced_diff",
        instruction=(
            "RAW_PROXY_FORCE_DIFF: update README.md, run configured verification, and "
            "the controller should force git_diff before final."
        ),
        setup=_setup_git_readme_repo,
        verify_cmd=(f"{shlex.quote(sys.executable)} -m py_compile src/calc.py",),
        oracle=_oracle_contains("README.md", "forced diff marker"),
    ),
}


def _select_scenarios(
    scenario_names: tuple[str, ...] | list[str] | None,
) -> tuple[RawAgentProxyScenario, ...]:
    if not scenario_names:
        return tuple(RAW_AGENT_PROXY_SCENARIOS.values())
    selected: list[RawAgentProxyScenario] = []
    for raw_name in scenario_names:
        for name in str(raw_name).split(","):
            normalized = name.strip()
            if not normalized:
                continue
            if normalized == "all":
                selected.extend(RAW_AGENT_PROXY_SCENARIOS.values())
                continue
            scenario = RAW_AGENT_PROXY_SCENARIOS.get(normalized)
            if scenario is None:
                available = ", ".join(sorted(RAW_AGENT_PROXY_SCENARIOS))
                raise KeyError(
                    f"Unknown raw-agent proxy scenario {normalized!r}; choose {available}"
                )
            selected.append(scenario)
    deduped: dict[str, RawAgentProxyScenario] = {}
    for scenario in selected:
        deduped.setdefault(scenario.name, scenario)
    return tuple(deduped.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run raw-agent proxy scenarios.")
    parser.add_argument("--output", type=Path, default=Path("qa_reports/raw_agent_proxy"))
    parser.add_argument(
        "--scenarios",
        nargs="*",
        help="Scenario names, comma-separated selectors, or all.",
    )
    parser.add_argument(
        "--scenario-timeout-s",
        type=float,
        default=DEFAULT_SCENARIO_TIMEOUT_S,
        help="Hard timeout for each raw-agent CLI subprocess.",
    )
    parser.add_argument(
        "--cli-command",
        nargs="+",
        help="Override the CLI command prefix, for example: alysis",
    )
    parser.add_argument(
        "--no-mock-provider",
        action="store_true",
        help="Do not start the local mock provider; use the caller's configured provider.",
    )
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    args = parser.parse_args(argv)
    if args.list:
        for name in sorted(RAW_AGENT_PROXY_SCENARIOS):
            print(name)
        return 0
    result = run_raw_agent_proxy_harness(
        output_dir=args.output,
        scenario_names=tuple(args.scenarios or ()),
        scenario_timeout_s=args.scenario_timeout_s,
        cli_command=tuple(args.cli_command) if args.cli_command else None,
        use_mock_provider=not args.no_mock_provider,
    )
    print(result.report_path)
    return 0 if result.metrics["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
