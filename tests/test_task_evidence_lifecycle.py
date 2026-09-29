from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.acceptance_contract import AcceptanceCriterionKind, EvidenceOrigin
from alysis_code.agent.task_state import recover_task_state_from_events, restore_session_task_state
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, *calls: tuple[str, dict[str, Any]]):
        self.calls = list(calls)
        self.serial = 0

    def chat(self, **_: Any) -> LLMResponse:
        self.serial += 1
        if self.calls:
            name, arguments = self.calls.pop(0)
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id=f"call-{self.serial}", name=name, arguments=arguments)],
                raw={},
            )
        return LLMResponse(content="The requested work is complete.", tool_calls=[], raw={})


def _session(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "source", checkpoint: bool = False
):
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    command = '"' + sys.executable.replace("\\", "/") + '" check.py'
    cfg = AppConfig(model="test-model", routing_mode="code_only", verify_commands=[command])
    cfg.anytime_checkpoint.enabled = checkpoint
    if checkpoint:
        cfg.anytime_checkpoint.objective_command = command
        cfg.anytime_checkpoint.max_candidates = 2
    session = create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=True,
        session_log_dir_override=root / "logs",
        session_id_override=name,
    )
    return session, command


def _rewrite_checker(session, command):
    session.client = _ScriptedClient(
        ("fs_read", {"path": "check.py"}),
        ("fs_write", {"path": "check.py", "content": "raise SystemExit(0)\n"}),
    )
    session.run_turn(
        f"Run `{command}` and report the result. Preserve check.py unchanged.",
        task_relation="new_task",
    )
    assert not session.last_turn_outcome["verified_success"]
    assert session.last_turn_outcome["generation"] == 1


@pytest.mark.parametrize("relation", ["continuation", "amendment"])
@pytest.mark.parametrize("resume", [False, True])
def test_rewritten_checker_cannot_become_authoritative_on_continuation_or_resume(
    tmp_path, monkeypatch, relation, resume
):
    (tmp_path / "check.py").write_text("raise SystemExit(3)\n")
    session, command = _session(tmp_path, monkeypatch)
    try:
        _rewrite_checker(session, command)
        original_id = session.task_state.task_id
        original_revision = session.last_turn_outcome["acceptance_revision"]
        original_snapshot = session._task_evidence_state.snapshot
        if resume:
            recovered = recover_task_state_from_events(
                session.store.events_snapshot(), session_id=session.store.session_id
            )
            old_session = session
            session, _ = _session(tmp_path, monkeypatch, name="resumed")
            restore_session_task_state(
                session,
                recovered,
                source_session_id=old_session.store.session_id,
                expected_session_id=old_session.store.session_id,
            )
            old_session.close()
        session.client = _ScriptedClient(("shell_run", {"cmd": command}))
        session.run_turn(
            "Continue the same task and run the required check."
            if relation == "continuation"
            else "Also preserve the logs directory unchanged.",
            task_relation=relation,
        )
        assert session.task_state.task_id == original_id
        assert session._task_evidence_state.snapshot == original_snapshot
        assert session.last_turn_outcome["generation"] == 1
        assert "check.py" in session.last_turn_outcome["material_paths"]
        assert not session.last_turn_outcome["verified_success"]
        contract = session._turn_execution_state.acceptance_contract
        assert any(
            item.kind == AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH
            and item.status.value == "FAILED"
            for item in contract.criteria
        )
        assert any(
            item.origin == EvidenceOrigin.SELF_AUTHORED
            for item in contract.evidence
            if item.command == command
        )
        assert (contract.acceptance_revision == original_revision) is (relation == "continuation")
    finally:
        session.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "foreign_writer", "unfinished_tool"])
def test_missing_or_untrusted_resumed_baseline_cannot_verify_current_checker(
    tmp_path, monkeypatch, damage
):
    (tmp_path / "check.py").write_text("raise SystemExit(0)\n")
    source, command = _session(tmp_path, monkeypatch)
    resumed = None
    try:
        source.client = _ScriptedClient(("shell_run", {"cmd": command}))
        source.run_turn(f"Run `{command}` and report the result.")
        assert source.last_turn_outcome["verified_success"]
        events = json.loads(json.dumps(source.store.events_snapshot()))
        if damage == "missing":
            events = [event for event in events if event["type"] != "session_task_evidence"]
        elif damage == "corrupt":
            next(event for event in reversed(events) if event["type"] == "session_task_evidence")[
                "payload"
            ]["snapshot"] = {}
        elif damage == "foreign_writer":
            next(event for event in reversed(events) if event["type"] == "session_task_evidence")[
                "session_id"
            ] = "another-session"
        else:
            events.append({"type": "tool_call", "payload": {"name": "fs_write"}})
        recovered = recover_task_state_from_events(events, session_id=source.store.session_id)
        resumed, _ = _session(tmp_path, monkeypatch, name="resumed")
        restore_session_task_state(
            resumed,
            recovered,
            source_session_id=source.store.session_id,
            expected_session_id=source.store.session_id,
        )
        resumed.client = _ScriptedClient(("shell_run", {"cmd": command}))
        resumed.run_turn("Continue the task and run the check.", task_relation="continuation")
        assert not resumed.last_turn_outcome["verified_success"]
        assert "acceptance_baseline_unavailable" in resumed.last_turn_outcome["problems"]
        assert not resumed._turn_execution_state.acceptance_contract.baseline_available
        assert not resumed._turn_execution_state.accepted_verification_evidence
    finally:
        source.close()
        if resumed:
            resumed.close()


def test_unchanged_checker_remains_authoritative_and_explicit_new_task_resets_baseline(
    tmp_path, monkeypatch
):
    (tmp_path / "check.py").write_text("raise SystemExit(3)\n")
    session, command = _session(tmp_path, monkeypatch)
    try:
        _rewrite_checker(session, command)
        old_task = session.task_state.task_id
        for relation in ("new_task", "continuation"):
            session.client = _ScriptedClient(("shell_run", {"cmd": command}))
            session.run_turn(
                f"Run `{command}` and report the result. Preserve check.py unchanged.",
                task_relation=relation,
            )
            assert session.task_state.task_id != old_task
            assert session.last_turn_outcome["verified_success"]
            assert session.last_turn_outcome["generation"] == 0
            assert not session.last_turn_outcome["material_paths"]
    finally:
        session.close()


@pytest.mark.parametrize("resume", [False, True])
def test_task_wide_generations_and_amended_requirements_scope_checkpoints(
    tmp_path, monkeypatch, resume
):
    (tmp_path / "check.py").write_text(
        'import json\nfrom pathlib import Path\nprint(json.dumps({"score": int(Path("score.txt").read_text())}))\n'
    )
    session, command = _session(tmp_path, monkeypatch, checkpoint=True)
    try:
        for score in (7, 3):
            calls = [("fs_write", {"path": "score.txt", "content": str(score)})]
            if score == 3:
                calls.insert(0, ("fs_read", {"path": "score.txt"}))
            session.client = _ScriptedClient(*calls, ("shell_run", {"cmd": command}))
            session.run_turn(
                f"Write score.txt and run `{command}`."
                if score == 7
                else "Continue by improving score.txt and run the check.",
                task_relation="new_task" if score == 7 else "continuation",
            )
            assert session.last_turn_outcome["verified_success"]
            assert session._anytime_checkpoints.best["objective_value"] == score
            if resume and score == 7:
                recovered = recover_task_state_from_events(
                    session.store.events_snapshot(), session_id=session.store.session_id
                )
                owner = session.store.session_id
                original_snapshot = session._task_evidence_state.snapshot
                session.close()
                session, _ = _session(tmp_path, monkeypatch, name=owner, checkpoint=True)
                restore_session_task_state(
                    session, recovered, source_session_id=owner, expected_session_id=owner
                )
                assert session._task_evidence_state.snapshot == original_snapshot
                assert session._acknowledged_checkpoint["objective_value"] == 7
        assert session.last_turn_outcome["generation"] == 2
        assert session._anytime_checkpoints.experiments_exhausted
        old_revision = session.last_turn_outcome["acceptance_revision"]
        session.client = _ScriptedClient()
        session.run_turn("Also create additional.txt.", task_relation="amendment")
        assert session.last_turn_outcome["acceptance_revision"] != old_revision
        assert session._anytime_checkpoints.best is None
        assert not session._anytime_checkpoints.experiments_exhausted
        assert "best_verified_checkpoint" not in session.last_turn_outcome
    finally:
        session.close()


def test_evidence_ledger_write_failure_cannot_grant_authority(tmp_path, monkeypatch):
    (tmp_path / "check.py").write_text("raise SystemExit(0)\n")
    session, command = _session(tmp_path, monkeypatch)
    append = session.store.append

    def failing_append(event, payload, **kwargs):
        if event == "session_task_evidence":
            raise OSError("evidence volume unavailable")
        return append(event, payload, **kwargs)

    monkeypatch.setattr(session.store, "append", failing_append)
    try:
        session.client = _ScriptedClient(("shell_run", {"cmd": command}))
        session.run_turn(f"Run `{command}` and report the result.")
        assert not session.last_turn_outcome["verified_success"]
        assert "acceptance_baseline_unavailable" in session.last_turn_outcome["problems"]
    finally:
        session.close()


def test_early_deadline_after_prior_turn_keeps_task_generation_without_reusing_proof(
    tmp_path, monkeypatch
):
    (tmp_path / "check.py").write_text("raise SystemExit(3)\n")
    session, command = _session(tmp_path, monkeypatch)
    try:
        _rewrite_checker(session, command)
        clock = [0.0]
        session.execution_deadline = ExecutionDeadline.from_duration(1, clock=lambda: clock[0])
        clock[0] = 2
        session.client = _ScriptedClient()
        session.run_turn("Continue the task.", task_relation="continuation")
        assert session.last_turn_outcome["outcome"] == "deadline_exceeded"
        assert session.last_turn_outcome["generation"] == 1
        assert session.last_turn_outcome["material_paths"] == ["check.py"]
        assert not session.last_turn_outcome["verified_success"]
        assert session.client.serial == 0
    finally:
        session.close()


def test_task_baseline_roundtrip_retains_entries_beyond_diagnostic_truncation(tmp_path):
    from types import SimpleNamespace

    from alysis_code.agent.acceptance_contract import AcceptanceWorkspaceSnapshot
    from alysis_code.agent.task_evidence import TaskEvidenceState, restore_task_evidence
    from alysis_code.verification_command_analysis import CheckerEntrypointFingerprint

    paths = frozenset(f"checks/check_{index}.py" for index in range(250))
    snapshot = AcceptanceWorkspaceSnapshot(
        preexisting_paths=paths,
        preexisting_checker_paths=paths,
        preexisting_checker_fingerprints=tuple(
            CheckerEntrypointFingerprint(path, str(tmp_path / path), True, 1, "0" * 64)
            for path in sorted(paths)
        ),
    )
    baseline = TaskEvidenceState("task", str(tmp_path.resolve()), snapshot)
    assert len(snapshot.as_payload()["preexisting_checker_fingerprints"]) == 200
    session = SimpleNamespace(
        root=tmp_path,
        task_state=SimpleNamespace(task_id="task"),
        store=SimpleNamespace(append=lambda *args: None),
    )
    restore_task_evidence(
        session, baseline.to_payload(), event_session_id="owner", expected_session_id="owner"
    )
    assert session._task_evidence_state.snapshot == snapshot
    assert session._task_evidence_state.baseline_available


@pytest.mark.parametrize("host_capped", [True, False])
def test_chat_only_provider_timeout_keeps_deadline_and_provider_failures_distinct(
    tmp_path, monkeypatch, host_capped
):
    clock = [0.0]
    session, _ = _session(tmp_path, monkeypatch)
    session.execution_deadline = ExecutionDeadline.from_duration(10, clock=lambda: clock[0])

    class Client:
        model = "test-model"
        temperature = 0.2
        timeout_s = 60 if host_capped else 1

        def chat(self, **kwargs):
            clock[0] = 10 if host_capped else 1
            raise LLMError("Normalized provider timeout") from TimeoutError("Read timed out")

    session.client = Client()
    try:
        if host_capped:
            assert session.run_turn("Explain this term.", chat_only=True) == 0
        else:
            with pytest.raises(LLMError):
                session.run_turn("Explain this term.", chat_only=True)
        assert session.last_turn_outcome["outcome"] == (
            "deadline_exceeded" if host_capped else "provider_failure"
        )
        assert not session.last_turn_outcome["verified_success"]
    finally:
        session.close()
