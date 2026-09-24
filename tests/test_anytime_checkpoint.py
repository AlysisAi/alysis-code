from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from alysis_code.agent.anytime_checkpoint import AnytimeCheckpointManager
from alysis_code.config import AnytimeCheckpointConfig
from alysis_code.run_outcome import task_outcome_record
from alysis_code.session_artifacts import SessionArtifactLayout


def _outcome(generation: int, *, passed: bool = True, task_id: str = "task-one"):
    return task_outcome_record(
        exit_code=0,
        reason="completed",
        task_id=task_id,
        state={
            "material_edit_count": 1,
            "verification_relevant_edit_generation": generation,
            "touched_repo_paths": ["deliverable.py"],
            "completion_certificate": {"status": "SUFFICIENT" if passed else "CONTRADICTED"},
            "accepted_verification_evidence": [{"evidence_category": "USER_EXPLICIT"}],
        },
    )


def _manager(tmp_path, **config):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return AnytimeCheckpointManager(
        root=root,
        layout=SessionArtifactLayout(tmp_path / "artifacts"),
        config=AnytimeCheckpointConfig(**config),
    )


def test_real_numeric_check_keeps_better_candidate_and_fresh_consumer_can_run_it(tmp_path):
    command = "python deliverable.py"
    manager = _manager(tmp_path, objective_command=command)
    for generation, score in enumerate([7, 3, 9]):
        (manager.root / "deliverable.py").write_text(
            f'import json\nprint(json.dumps({{"score": {score}}}))\n'
        )
        process = subprocess.run(
            [sys.executable, "deliverable.py"],
            cwd=manager.root,
            capture_output=True,
            text=True,
            check=True,
        )
        event = manager.consider(
            outcome=_outcome(generation),
            result={"cmd": command, "exit_code": process.returncode, "stdout": process.stdout},
            accepted_commands={command},
        )
        assert event
    assert event["status"] == "prior_candidate_retained"
    assert manager.best["objective_value"] == 3
    consumer = tmp_path / "fresh-consumer"
    with zipfile.ZipFile(manager.best["archive_path"]) as archive:
        archive.extractall(consumer)
    result = subprocess.run(
        [sys.executable, "deliverable.py"], cwd=consumer, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout)["score"] == 3
    assert "9" in (manager.root / "deliverable.py").read_text()
    # Resume validates archive identity and keeps its original version.
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("task-one", acknowledged_checkpoint=manager.best)
    assert resumed.best == manager.best
    resumed.select_task("a-foreign-task")
    assert resumed.best is None


def test_failed_unmeasured_and_model_claimed_scores_never_replace_verified_candidate(tmp_path):
    manager = _manager(tmp_path)
    (manager.root / "deliverable.py").write_bytes(b'print("working")\n')
    manager.consider(outcome=_outcome(0), result={}, accepted_commands=set())
    original = manager.best.copy()
    (manager.root / "deliverable.py").write_text('raise RuntimeError("broken")\n')
    assert (
        manager.consider(
            outcome=_outcome(1, passed=False), result={"score": 100}, accepted_commands=set()
        )["status"]
        == "unverified_candidate_rejected"
    )
    manager.consider(outcome=_outcome(2), result={"score": 100}, accepted_commands=set())
    assert manager.best == original
    with zipfile.ZipFile(original["archive_path"]) as archive:
        assert archive.read("deliverable.py") == b'print("working")\n'


def test_checkpoint_excludes_credentials_and_rejects_expired_or_oversized_candidate(tmp_path):
    manager = _manager(tmp_path, max_total_bytes=1024)
    (manager.root / "deliverable.py").write_text("pass\n")
    (manager.root / ".env").write_text("SECRET=private\n")
    manager.consider(outcome=_outcome(0), result={}, accepted_commands=set())
    with zipfile.ZipFile(manager.best["archive_path"]) as archive:
        assert ".env" not in archive.namelist()
    assert ".env" in manager.best["omitted_paths"]
    original = manager.best.copy()
    (manager.root / "deliverable.py").write_bytes(b"x" * 2048)
    other = _manager(tmp_path, max_total_bytes=1024)
    event = other.consider(outcome=_outcome(1, task_id="new"), result={}, accepted_commands=set())
    assert event["status"] == "checkpoint_unavailable"
    assert "size_budget" in event["reason"]
    expired = other.consider(
        outcome=_outcome(2, task_id="new"), result={}, accepted_commands=set(), remaining_seconds=0
    )
    assert expired["status"] == "checkpoint_time_budget_exhausted"
    assert manager.best == original


def test_mutated_archive_cannot_be_resumed_as_verified(tmp_path):
    manager = _manager(tmp_path)
    (manager.root / "deliverable.py").write_text("pass\n")
    manager.consider(outcome=_outcome(0), result={}, accepted_commands=set())
    Path(manager.best["archive_path"]).write_bytes(b"corrupted")
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("task-one", acknowledged_checkpoint=manager.best)
    assert resumed.best is None


@pytest.mark.parametrize("stop", ["provider", "deadline"])
def test_real_turn_preserves_verified_program_when_later_candidate_and_provider_fail(
    tmp_path, monkeypatch, stop
):
    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    (tmp_path / "check.py").write_text(
        "import deliverable\nassert deliverable.value == 7\nprint('check passed')\n"
    )
    command = '"' + sys.executable.replace("\\", "/") + '" check.py'

    class Client:
        model = "test-model"
        temperature = 0.2
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                tool = ToolCall(
                    id="good",
                    name="fs_write",
                    arguments={"path": "deliverable.py", "content": "value = 7\n"},
                )
            elif self.calls == 2:
                tool = ToolCall(id="check", name="shell_run", arguments={"cmd": command})
            elif self.calls == 3:
                tool = ToolCall(
                    id="worse",
                    name="fs_write",
                    arguments={"path": "deliverable.py", "content": "value = 999\n"},
                )
            else:
                if stop == "deadline":
                    from alysis_code.cancellation import CooperativeCancellationError

                    raise CooperativeCancellationError("run_budget_exhausted")
                raise LLMError("Provider unavailable: connection refused after retries")
            return LLMResponse(content="", tool_calls=[tool], raw={})

    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", verify_commands=[command]),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=True,
        session_log_dir_override=tmp_path / "logs",
    )
    session.client = Client()
    try:
        session.run_turn(f"Implement deliverable.py with value 7 and verify using `{command}`.")
        outcome = session.last_turn_outcome
        assert outcome["verified_success"] is False
        assert outcome["outcome"] == (
            "provider_failure" if stop == "provider" else "deadline_exceeded"
        )
        best = outcome["best_verified_checkpoint"]
        with zipfile.ZipFile(best["archive_path"]) as archive:
            assert archive.read("deliverable.py") == b"value = 7\n"
            assert not any(name.startswith("logs/") for name in archive.namelist())
        assert (tmp_path / "deliverable.py").read_text() == "value = 999\n"
        finals = [
            event["payload"]["content"]
            for event in session.store.events_snapshot()
            if event["type"] == "final"
        ]
        assert best["archive_path"] in finals[-1]
    finally:
        session.close()


def test_configured_candidate_budget_survives_resume_and_counts_failed_experiment(tmp_path):
    manager = _manager(tmp_path, objective_command="python measure.py", max_candidates=2)
    (manager.root / "deliverable.py").write_text("pass\n")
    manager.consider(outcome=_outcome(0), result={}, accepted_commands=set())
    assert not manager.experiments_exhausted
    manager.consider(outcome=_outcome(1, passed=False), result={}, accepted_commands=set())
    assert manager.experiments_exhausted
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("task-one", acknowledged_checkpoint=manager.best)
    assert resumed.experiments_exhausted
