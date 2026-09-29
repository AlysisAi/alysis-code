from __future__ import annotations

import asyncio
import shlex
from types import SimpleNamespace

import pytest

from scripts.benchmarks.terminal_bench import alysis_agent, box_harbor_agent, harbor_agent

ANCHOR = "ALYSIS_MANAGED_HOST_DEADLINE_UNIX_SECONDS"


def _adapter_launch(kind, tmp_path, monkeypatch, clock, existing):
    monkeypatch.setattr(alysis_agent.time, "time", lambda: clock[0])
    monkeypatch.setattr(alysis_agent.time, "monotonic", lambda: clock[0])
    if existing is None:
        monkeypatch.delenv(ANCHOR, raising=False)
    else:
        monkeypatch.setenv(ANCHOR, existing)
    calls = []

    async def execute(_environment, **kwargs):
        calls.append({**kwargs, "env": dict(kwargs["env"]), "dispatch_time": clock[0]})
        return SimpleNamespace(exit_code=0, stdout="")

    if kind == "box":
        monkeypatch.setattr(
            box_harbor_agent,
            "evaluate_adapter_provenance",
            lambda **_: SimpleNamespace(payload=lambda: {"allowed": True}, failed=False),
        )
        agent = box_harbor_agent.AlysisAgent(
            logs_dir=tmp_path / "logs",
            extra_env={
                "ALYSIS_MODEL": "test-model",
                "ALYSIS_API_KEY": "test-secret",
                "ALYSIS_BASE_URL": "http://127.0.0.1:1",
            },
        )
        agent.exec_as_agent = execute

        def launch():
            context = SimpleNamespace(
                metadata={"managed_host_deadline": {"remaining_seconds": 100}}
            )
            asyncio.run(agent.run("Check the public artifact.", object(), context))
            return calls[-1]

        persistent_env = agent._container_env
    elif kind == "harbor":
        agent = harbor_agent.AlysisHarborAgent(
            logs_dir=tmp_path / "logs",
            api_key="test-secret",
            model_name="test-model",
            base_url="http://127.0.0.1:1",
            command_timeout_sec=100,
            shutdown_reserve_sec=10,
        )
        agent.exec_as_agent = execute

        async def copy(_environment):
            return None

        agent._copy_runtime_artifacts = copy

        def launch():
            asyncio.run(
                agent.run("Check the public artifact.", object(), SimpleNamespace(metadata={}))
            )
            return calls[-1]

        persistent_env = agent._runtime_env
    else:
        agent = alysis_agent.AlysisSimpleAgent(
            api_key="test-secret",
            model_name="test-model",
            managed_host_agent_timeout_sec=100,
            managed_host_shutdown_reserve_sec=10,
        )

        def launch():
            command = agent._run_agent_commands("Check the public artifact.")[0]
            assignment, cli = command.command.split(" ", 1)
            key, value = assignment.split("=", 1)
            return {
                "command": cli,
                "env": {key: value},
                "dispatch_time": clock[0],
                "timeout_sec": command.max_timeout_sec,
            }

        def persistent_env():
            return agent._env

    return launch, calls, persistent_env


def _relative_seconds(command):
    parts = shlex.split(command.split("alysis run", 1)[1] if "alysis run" in command else command)
    return float(parts[parts.index("--deadline-seconds") + 1])


@pytest.mark.parametrize("kind", ["box", "harbor", "terminal"])
@pytest.mark.parametrize("existing", [None, "2010", "2500", "1900"])
def test_managed_launch_anchor_never_extends_inherited_deadline(
    kind, existing, tmp_path, monkeypatch
):
    clock = [2000.0]
    launch, calls, persistent_env = _adapter_launch(kind, tmp_path, monkeypatch, clock, existing)
    result = launch()
    relative = _relative_seconds(result["command"])
    expected = result["dispatch_time"] + relative
    if existing is not None:
        expected = min(expected, float(existing))
    assert float(result["env"][ANCHOR]) == pytest.approx(expected, abs=0.00001)
    assert "--require-deadline" in result["command"]
    assert result["timeout_sec"] > relative
    assert ANCHOR not in persistent_env()
    if kind == "box":
        assert all(ANCHOR not in call["env"] for call in calls[:-1])


@pytest.mark.parametrize("kind", ["box", "harbor", "terminal"])
@pytest.mark.parametrize("existing", ["", "nan", "invalid"])
def test_invalid_inherited_anchor_never_dispatches_agent(kind, existing, tmp_path, monkeypatch):
    launch, calls, _ = _adapter_launch(kind, tmp_path, monkeypatch, [2000.0], existing)
    with pytest.raises(ValueError, match="deadline|anchor|UNIX"):
        launch()
    assert all("alysis run" not in call["command"] for call in calls)


@pytest.mark.parametrize("kind", ["box", "harbor", "terminal"])
def test_managed_anchor_is_fresh_for_each_independent_launch(kind, tmp_path, monkeypatch):
    clock = [2000.0]
    launch, _, persistent_env = _adapter_launch(kind, tmp_path, monkeypatch, clock, None)
    first = launch()
    clock[0] += 500
    second = launch()
    assert float(second["env"][ANCHOR]) - float(first["env"][ANCHOR]) == pytest.approx(
        500, abs=0.05
    )
    assert ANCHOR not in persistent_env()
