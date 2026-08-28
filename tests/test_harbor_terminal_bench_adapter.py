from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.run_outcome import INFRASTRUCTURE_FAILURE_EXIT_CODE, RunOutcome
from scripts.benchmarks.terminal_bench import box_harbor_agent as box_adapter_mod
from scripts.benchmarks.terminal_bench.box_harbor_agent import AlysisAgent as BoxAlysisAgent
from scripts.benchmarks.terminal_bench.harbor_agent import AlysisHarborAgent


def _agent(**kwargs: Any) -> AlysisHarborAgent:
    defaults: dict[str, Any] = {
        "logs_dir": ".",
        "api_key": "SECRET-KEY",
        "model_name": "qwen-test",
        "base_url": "https://example.invalid/v1",
        "command_timeout_sec": 7200,
        "shutdown_reserve_sec": 120,
    }
    defaults.update(kwargs)
    return AlysisHarborAgent(**defaults)


def test_harbor_adapter_builds_command_with_1000_steps() -> None:
    instruction = "--starts-with-dash and quotes 'x'"
    command = _agent()._build_run_command(instruction)
    parts = shlex.split(command, posix=True)

    assert parts[:2] == ["alysis", "run"]
    assert parts[parts.index("--max-steps") + 1] == "1000"
    assert parts[parts.index("--deadline-seconds") + 1] == "7080"
    assert "--require-deadline" in parts
    assert "--subagents" in parts
    assert parts[parts.index("--api-key-env") + 1] == "ALYSIS_API_KEY"
    assert parts[parts.index("--") + 1 :] == [instruction]
    assert "SECRET-KEY" not in command


def test_harbor_adapter_can_disable_subagents() -> None:
    command = _agent(subagents=False)._build_run_command("do work")
    parts = shlex.split(command, posix=True)

    assert "--subagents" not in parts
    assert "--no-subagents" in parts


def test_harbor_runtime_env_uses_dashscope_key_without_putting_it_in_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "DASH-SECRET")
    agent = _agent(api_key=None)

    env = agent._runtime_env()
    command = agent._build_run_command("do work")

    assert env["ALYSIS_API_KEY"] == "DASH-SECRET"
    assert env["ALYSIS_BASE_URL"] == "https://example.invalid/v1"
    assert "DASH-SECRET" not in command


def test_harbor_runtime_env_accepts_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "OR-SECRET")
    agent = _agent(api_key=None, base_url="https://openrouter.ai/api/v1")

    env = agent._runtime_env()
    command = agent._build_run_command("do work")

    assert env["ALYSIS_API_KEY"] == "OR-SECRET"
    assert env["ALYSIS_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert "OR-SECRET" not in command


def test_harbor_runtime_env_fails_closed_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    agent = _agent(api_key=None)

    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        agent._runtime_env()


def test_box_adapter_install_uses_versioned_setup_script_and_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    agent = BoxAlysisAgent(
        extra_env={
            "ALYSIS_WHEEL": str(wheel),
            "ALYSIS_MODEL": "mimo-v2.5-pro",
            "ALYSIS_BASE_URL": "https://example.invalid/v1",
            "ALYSIS_API_KEY": "SECRET-KEY",
        }
    )

    install_env = agent._install_env()
    install_command = agent._install_command()

    assert agent._host_setup_script_path().endswith("scripts/benchmarks/terminal_bench/setup.sh")
    assert install_env["ALYSIS_WHEEL"] == "/tmp/alysis-agent/" + wheel.name
    assert install_env["ALYSIS_MODEL"] == "mimo-v2.5-pro"
    assert install_env["ALYSIS_BASE_URL"] == "https://example.invalid/v1"
    assert install_env["ALYSIS_SETUP_LOG_DIR"] == "/logs/agent/setup"
    assert install_env["ALYSIS_SETUP_ARTIFACT_DIR"] == "/logs/artifacts/setup"
    assert "ALYSIS_API_KEY" not in install_env
    assert "setup.sh" in install_command
    assert "SECRET-KEY" not in install_command


def test_box_adapter_summary_marks_infrastructure_exit_separately(tmp_path: Path) -> None:
    wheel = tmp_path / "alysis-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    agent = BoxAlysisAgent(
        extra_env={
            "ALYSIS_WHEEL": str(wheel),
            "ALYSIS_MODEL": "mimo-v2.5-pro",
            "ALYSIS_BASE_URL": "https://example.invalid/v1",
            "ALYSIS_API_KEY": "SECRET-KEY",
        }
    )
    calls = 0

    async def fake_exec_as_agent(
        _environment: object,
        *,
        command: str,
        env: dict[str, str],
        **_kwargs: object,
    ) -> None:
        nonlocal calls
        _ = command, env
        calls += 1
        if calls == 3:
            raise RuntimeError(
                f"Command failed (exit {INFRASTRUCTURE_FAILURE_EXIT_CODE}): provider outage"
            )

    agent.exec_as_agent = fake_exec_as_agent  # type: ignore[attr-defined,method-assign]
    context = SimpleNamespace(metadata={})

    with pytest.raises(RuntimeError, match="provider outage"):
        asyncio.run(agent.run("fix the bug", object(), context))

    assert context.metadata["alysis_exit_code"] == INFRASTRUCTURE_FAILURE_EXIT_CODE
    assert context.metadata["alysis_outcome"] == RunOutcome.INFRA_FAIL.value


def test_terminal_bench_setup_script_retries_network_installs_and_uses_venv() -> None:
    setup_text = (Path(box_adapter_mod.__file__).with_name("setup.sh")).read_text(encoding="utf-8")

    assert setup_text.startswith("#!/bin/sh")
    assert "/logs/agent/setup" in setup_text
    assert "/logs/artifacts/setup" in setup_text
    assert "retry apt-get-update apt-get update" in setup_text
    assert "retry apk-add apk add" in setup_text
    assert "retry dnf-install dnf install" in setup_text
    assert "retry uv-installer" in setup_text
    assert "retry pip-install-alysis" in setup_text
    assert "retry uv-pip-install" in setup_text
    assert "uv python install 3.12" in setup_text
    assert "python3 -m venv /opt/alysis-venv" in setup_text
    assert "ln -sf /opt/alysis-venv/bin/alysis /usr/local/bin/alysis" in setup_text
    assert "ALYSIS_WHEEL" in setup_text
    assert "--break-system-packages" not in setup_text


def test_harbor_runner_raises_verifier_timeout_multiplier() -> None:
    runner_text = (Path(box_adapter_mod.__file__).with_name("run_harbor_tbench.sh")).read_text(
        encoding="utf-8"
    )

    assert 'TB_VERIFIER_TIMEOUT_MULTIPLIER="${TB_VERIFIER_TIMEOUT_MULTIPLIER:-10}"' in runner_text
