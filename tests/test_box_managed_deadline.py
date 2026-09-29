from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.benchmarks.terminal_bench import box_harbor_agent as adapter


def _agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    monkeypatch.setattr(
        adapter,
        "evaluate_adapter_provenance",
        lambda **_: SimpleNamespace(
            payload=lambda: {"allowed": True},
            failed=False,
        ),
    )
    return adapter.AlysisAgent(
        logs_dir=tmp_path / "logs",
        extra_env={
            "ALYSIS_MODEL": "local-test",
            "ALYSIS_API_KEY": "PRIVATE",
            "ALYSIS_BASE_URL": "http://127.0.0.1:1",
            **env,
        },
    )


def _run(
    agent: Any,
    metadata: dict[str, Any],
    *,
    elapsed_per_setup: float = 0,
    clock: list[float] | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    calls: list[dict[str, Any]] = []

    async def execute(_environment: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        if clock is not None and len(calls) <= 2:
            clock[0] += elapsed_per_setup

    agent.exec_as_agent = execute
    context = SimpleNamespace(metadata=metadata)
    asyncio.run(agent.run("Build the user's artifact.", object(), context))
    return calls, context


def _deadline(command: str) -> float:
    args = shlex.split(command.split("alysis run", 1)[1].split("</dev/null", 1)[0])
    assert args.count("--require-deadline") == 1
    return float(args[args.index("--deadline-seconds") + 1])


@pytest.mark.parametrize("label", ["overfull-hbox__old", "unseen-production-job__new", "arbitrary"])
def test_metadata_budget_is_independent_of_job_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    agent = _agent(tmp_path, monkeypatch, ALYSIS_TBENCH_TASK_NAME=label)
    calls, context = _run(agent, {"managed_host_deadline": {"remaining_seconds": 3000}})
    assert _deadline(calls[-1]["command"]) == 2550
    assert float(calls[-1]["env"]["ALYSIS_RUN_BUDGET_SECONDS"]) == 2550
    assert calls[-1]["timeout_sec"] == 3000
    assert context.metadata["budget_table_hit"] is False
    assert "task_name" not in context.metadata


def test_setup_time_is_deducted_once_from_same_host_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [10.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    agent = _agent(tmp_path, monkeypatch)
    calls, context = _run(
        agent,
        {"managed_host_deadline": {"remaining_seconds": 100}},
        elapsed_per_setup=7,
        clock=clock,
    )
    assert _deadline(calls[-1]["command"]) == 56
    assert calls[-1]["timeout_sec"] == 86
    assert context.metadata["elapsed_before_launch_seconds"] == 14
    assert context.metadata["host_shutdown_reserve_seconds"] == 30


@pytest.mark.parametrize("persistence_seconds", [25, 80])
def test_slow_provenance_does_not_renew_the_host_launch_window(
    tmp_path, monkeypatch, persistence_seconds
):
    clock = [100.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(adapter.time, "time", lambda: clock[0] + 1900)
    monkeypatch.delenv("ALYSIS_MANAGED_HOST_DEADLINE_UNIX_SECONDS", raising=False)
    agent = _agent(tmp_path, monkeypatch)
    original_write = agent._write_provenance_artifact

    def slow_write():
        clock[0] += persistence_seconds
        original_write()

    monkeypatch.setattr(agent, "_write_provenance_artifact", slow_write)
    calls = []

    async def execute(_environment, **kwargs):
        calls.append(kwargs)

    agent.exec_as_agent = execute
    context = SimpleNamespace(metadata={"managed_host_deadline": {"remaining_seconds": 100}})
    if persistence_seconds == 80:
        with pytest.raises(ValueError, match="too small"):
            asyncio.run(agent.run("work", object(), context))
        assert len(calls) == 2
        assert all("alysis run" not in call["command"] for call in calls)
    else:
        asyncio.run(agent.run("work", object(), context))
        assert calls[-1]["timeout_sec"] == 75
        assert float(calls[-1]["env"]["ALYSIS_MANAGED_HOST_DEADLINE_UNIX_SECONDS"]) == 2070
        assert context.metadata["elapsed_before_launch_seconds"] == 25
        assert context.metadata["deadline_seconds_forwarded"] == 45


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"remaining_seconds": 0},
        {"remaining_seconds": -1},
        {"remaining_seconds": "nan"},
        {"remaining_seconds": "inf"},
        {"remaining_seconds": 20},
        {"deadline_unix_seconds": "nan"},
        {"deadline_unix_seconds": "invalid"},
    ],
)
def test_missing_invalid_or_expired_metadata_refuses_before_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: dict[str, Any]
) -> None:
    agent = _agent(tmp_path, monkeypatch, ALYSIS_RUN_BUDGET_SECONDS="10800")
    with pytest.raises(ValueError, match="Managed-host deadline"):
        _run(agent, {"managed_host_deadline": metadata})
    record = json.loads((tmp_path / "logs" / "alysis-provenance.json").read_text())
    assert record["managed_host_deadline"]["status"] == "blocked"
    assert "PRIVATE" not in json.dumps(record)


def test_setup_expiry_does_not_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [10.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    agent = _agent(tmp_path, monkeypatch)
    calls: list[str] = []

    async def execute(_environment: Any, **kwargs: Any) -> None:
        calls.append(kwargs["command"])
        clock[0] += 80

    agent.exec_as_agent = execute
    with pytest.raises(ValueError, match="too small"):
        asyncio.run(
            agent.run(
                "work",
                object(),
                SimpleNamespace(
                    metadata={"managed_host_deadline": {"remaining_seconds": 100}},
                ),
            )
        )
    assert len(calls) == 1
    assert not any("alysis run" in command for command in calls)


@pytest.mark.parametrize("ceiling, expected", [("1000", 1000), ("10800", 2550), ("999999", 2550)])
def test_legacy_budget_is_only_a_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str, expected: int
) -> None:
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    agent = _agent(tmp_path, monkeypatch, ALYSIS_RUN_BUDGET_SECONDS=ceiling)
    calls, _ = _run(agent, {"managed_host_deadline": {"remaining_seconds": 3000}})
    assert _deadline(calls[-1]["command"]) == expected


def test_absolute_host_deadline_accounts_for_prelaunch_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter.time, "time", lambda: 5000.0)
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    agent = _agent(tmp_path, monkeypatch)
    calls, _ = _run(agent, {"managed_host_deadline": {"deadline_unix_seconds": 5100}})
    assert _deadline(calls[-1]["command"]) == 70


@pytest.mark.parametrize(
    "extra", ["--no-deadline", "--deadline-seconds=999999", "-- --no-deadline"]
)
def test_extra_arguments_cannot_override_the_host_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    agent = _agent(tmp_path, monkeypatch, ALYSIS_EXTRA_ARGS=extra)
    with pytest.raises(ValueError, match="cannot override"):
        _run(agent, {"managed_host_deadline": {"remaining_seconds": 100}})


def test_box_adapter_has_no_runtime_budget_table_dependency() -> None:
    source = Path(adapter.__file__).read_text()
    assert "task_budgets" not in source
    assert "budget_lookup" not in source


def test_host_timeout_cancels_stalled_setup_before_agent_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(tmp_path, monkeypatch)
    retired: list[bool] = []

    async def blocked(_environment: Any, **kwargs: Any) -> None:
        assert "alysis run" not in kwargs["command"]
        try:
            await asyncio.Event().wait()
        finally:
            retired.append(True)

    agent.exec_as_agent = blocked
    context = SimpleNamespace(
        metadata={
            "managed_host_deadline": {
                "remaining_seconds": 1.5,
                "shutdown_reserve_seconds": 0,
            }
        }
    )
    with pytest.raises(TimeoutError):
        asyncio.run(agent.run("work", object(), context))
    assert retired == [True]
    assert context.metadata["status"] == "deadline_exceeded"
    assert context.metadata["alysis_task_outcome"] is None


@pytest.mark.parametrize(
    "outcome", ["verified_success", "completed_unverified", "deadline_exceeded"]
)
def test_adapter_consumes_exact_root_outcome_independently_of_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    import re

    agent = _agent(tmp_path, monkeypatch)
    paths: list[str] = []

    async def execute(_environment: Any, **kwargs: Any) -> Any:
        if "alysis run" not in kwargs["command"]:
            return None
        paths.append(kwargs["env"]["ALYSIS_TASK_OUTCOME_PATH"])
        marker = re.search(r"ALYSIS_TASK_OUTCOME_[a-f0-9]{32}=", kwargs["command"])[0]
        return SimpleNamespace(
            stdout=marker
            + json.dumps(
                {
                    "session_id": "root-session",
                    "task_id": "root-task",
                    "terminal": True,
                    "outcome": outcome,
                    "verified_success": outcome == "verified_success",
                    "exit_code": 0,
                }
            )
        )

    agent.exec_as_agent = execute
    for _ in range(2):
        context = SimpleNamespace(metadata={"managed_host_deadline": {"remaining_seconds": 100}})
        asyncio.run(agent.run("work", object(), context))
        assert context.metadata["alysis_task_outcome"]["outcome"] == outcome
    assert paths[0] != paths[1]


def test_harbor_launch_rejects_missing_host_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.benchmarks.terminal_bench.harbor_agent import AlysisHarborAgent

    for key in ("ALYSIS_TBENCH_COMMAND_TIMEOUT_SEC", "TB_AGENT_TIMEOUT_SEC"):
        monkeypatch.delenv(key, raising=False)
    agent = AlysisHarborAgent(model_name="local", base_url="http://127.0.0.1:1")
    with pytest.raises(ValueError, match="required for managed-host"):
        agent._build_run_command("work")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"terminal": False, "outcome": "verified_success"},
        {"terminal": True, "outcome": "unknown", "session_id": "x"},
        {
            "terminal": True,
            "outcome": "verified_success",
            "session_id": "x",
            "verified_success": False,
        },
        {
            "terminal": True,
            "outcome": "provider_failure",
            "session_id": "x",
            "verified_success": True,
        },
    ],
)
def test_missing_or_nonterminal_sidecar_cannot_become_verified_success(payload: Any) -> None:
    from scripts.benchmarks.terminal_bench.adapter_outcome import outcome_metadata

    result = SimpleNamespace(stdout="PREFIX=" + json.dumps(payload))
    assert outcome_metadata(result, marker="PREFIX=")["alysis_task_outcome"] is None
