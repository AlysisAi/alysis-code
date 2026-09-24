"""Third independent review of the host-owned task identity (tree 90b42153):
the reviewer's seven probes integrated into the repository suite, plus the
sequence coverage behind the corrected contracts.

H3 — a recorded refusal stays authoritative through later logging, runtime
error handling, reopen and sidecar-to-marker reconciliation; a failed write
the store cannot locate is never guessed at.
H1 — an accepted amendment is exact content (order, repeats, blank lines,
indentation, line endings); the exact accepted requirements must be in the
outgoing model context before the model acts, or the turn is refused with an
explicit needs-input condition.
H2 — a present ``null`` list field is invalid; an omitted one stays the
documented default.

Deterministic clients only; every request is snapshotted at call time.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import alysis_code.session_store as stores
from alysis_code.agent.prompt_context import (
    _TASK_REQUIREMENTS_MAX_CHARS,
    _session_task_requirements_content,
    undelivered_task_requirements,
)
from alysis_code.agent.task_state import (
    TASK_STATE_EVENT,
    AcceptedAmendments,
    SessionTaskState,
    recover_task_state_from_events,
    transition_task_state,
    validate_task_state_payload,
)
from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.chat import loop as chat_loop_mod
from alysis_code.llm.openai_compat import LLMError
from alysis_code.session_store import (
    SESSION_STORE_UNCOMMITTED_EVENT,
    SessionStore,
    SessionStoreWriteError,
    read_session_events,
)
from tests.test_task_identity_hardening import (
    _dispatch_command,
    _events,
    _FailingClient,
    _final,
    _RecordingClient,
    _resume_into,
    _session,
    _single_brief,
    _single_requirements,
    _tool,
    _user_texts,
)
from tests.test_task_identity_lifecycle import _compaction_cfg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(objective: str = "Task A.", sequence: int = 1, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = dict(
        task_id=f"s:task:{sequence}",
        session_id="s",
        sequence=sequence,
        objective=objective,
        origin_event_id="s:1",
        amendments=("Preserve public APIs.",),
    )
    fields.update(overrides)
    return SessionTaskState(**fields).to_payload()


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        enabled=True, sessions_dir=tmp_path, session_id="s", cwd=str(tmp_path), repo_root=None
    )


def _replay(store: SessionStore) -> list[dict[str, Any]]:
    return list(read_session_events(store.path))


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("type") or "") for event in events]


def _disk_task(store: SessionStore) -> Any:
    return recover_task_state_from_events(_replay(store), session_id=store.session_id)


def _memory_task(store: SessionStore) -> Any:
    return recover_task_state_from_events(store.events_snapshot(), session_id=store.session_id)


def _assert_disk_memory_reopen_agree(tmp_path: Path, store: SessionStore) -> list[dict[str, Any]]:
    """Live snapshot, disk replay and a freshly reopened store all agree."""

    replay = _replay(store)
    assert replay == store.events_snapshot()
    reopened = _store(tmp_path)
    try:
        assert reopened.events_snapshot() == replay
        assert reopened.integrity_uncertain is False
        assert not reopened._uncommitted_sidecar_path.exists()
    finally:
        reopened.close()
    return replay


class _FlushErrorAfterCompleteWrite:
    """Bytes reach the file, then the flush that would complete the append fails."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def write(self, text: str) -> int:
        return self.inner.write(text)

    def flush(self) -> None:
        self.inner.flush()
        raise OSError("injected flush completion error")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _PartialWriteThenError:
    """Only a malformed prefix of the record reaches the file."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def write(self, text: str) -> int:
        self.inner.write(text[: len(text) // 2])
        self.inner.flush()
        raise OSError("injected partial record failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _TaskReplacementFlushFailure:
    """Fails only the ``replaced`` task-state record, after its bytes reached the file."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.fail = False

    def write(self, text: str) -> int:
        try:
            event = json.loads(text)
            self.fail = (
                event.get("type") == TASK_STATE_EVENT
                and event.get("payload", {}).get("transition") == "replaced"
            )
        except ValueError:
            self.fail = False
        return self.inner.write(text)

    def flush(self) -> None:
        self.inner.flush()
        if self.fail:
            raise OSError("injected task replacement flush failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _deny_truncate(monkeypatch: pytest.MonkeyPatch) -> None:
    def cannot_truncate(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected rollback truncation failure")

    monkeypatch.setattr(stores.os, "truncate", cannot_truncate)


class _MarkerAppendDenied:
    """Deny ``Path.open(<log>, "a")`` — the marker append and the log reopen —
    while ``active``; the sidecar file stays writable."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, log_path: Path, *, times: int | None = None
    ):
        self.log_path = log_path
        self.active = True
        self.remaining = times
        self.denied = 0
        real_open = Path.open
        guard = self

        def deny(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if guard.active and path == guard.log_path and "a" in mode:
                if guard.remaining is None or guard.remaining > 0:
                    guard.denied += 1
                    if guard.remaining is not None:
                        guard.remaining -= 1
                    raise OSError("injected marker-open failure")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny)


def _bind_chat_loop_globals(session: Any, tmp_path: Path) -> None:
    """A command dispatch through the CLI facade binds the chat loop's runtime
    globals exactly as production does (needed before calling the reset path
    directly)."""

    _dispatch_command("/objective", session=session, tmp_path=tmp_path)


def _rollover(session: Any, tmp_path: Path) -> None:
    _bind_chat_loop_globals(session, tmp_path)
    chat_loop_mod._reset_chat_conversation(
        session=session,
        trigger="captured_duplicate_subagent_result",
        retained_system_messages=("<subagent_lifecycle_capsule>kept</subagent_lifecycle_capsule>",),
    )


# ===========================================================================
# H3 — refusal survives subsequent logging; no guessed failed-record identity
# ===========================================================================


def test_h3_sidecar_refusal_remains_refused_after_next_successful_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe, extended with read → reopen → read."""

    store = _store(tmp_path)
    try:
        original = _payload()
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": original})
        replacement = copy.deepcopy(original)
        replacement.update(objective="Task B.", sequence=2, task_id="s:task:2")
        with monkeypatch.context() as m:
            _MarkerAppendDenied(m, store.path)
            _deny_truncate(m)
            store._fh = _FlushErrorAfterCompleteWrite(store._fh)  # type: ignore[assignment]
            with pytest.raises(SessionStoreWriteError) as error:
                store.append(TASK_STATE_EVENT, {"transition": "replaced", "state": replacement})
            assert error.value.outcome == "refused"
            assert store._uncommitted_sidecar_path.exists()
            # Durable refusal, marker still owed: no record may follow yet.
            assert store.integrity_uncertain is True
            assert _disk_task(store).state.objective == "Task A."
            with pytest.raises(SessionStoreWriteError) as blocked:
                store.append("warning", {"warning": "still blocked"})
            assert blocked.value.outcome == "indeterminate"
        # Ordinary logging after recovery, still in the same process: the
        # marker is folded in first, then the warning is appended.
        store.append("warning", {"warning": "the previous task acceptance failed"})
        assert store.integrity_uncertain is False
        assert not store._uncommitted_sidecar_path.exists()
        assert '"Task B."' in store.path.read_text(encoding="utf-8")
        assert _disk_task(store).state == _memory_task(store).state
        assert _disk_task(store).state.objective == "Task A."
        replay = _assert_disk_memory_reopen_agree(tmp_path, store)
        assert _types(replay) == [TASK_STATE_EVENT, "warning"]
    finally:
        store.close()


def test_h3_real_failed_turn_with_sidecar_does_not_resurrect_the_rejected_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe: the failed turn's own error/warning records must not
    make the rejected task authoritative — extended through /resume."""

    session = _session(tmp_path, mode="readonly", session_id_override="third-review-failed-turn")
    try:
        session.client = _RecordingClient([_final("Started task A.")])
        assert session.run_turn("Task A: review the CLI.") == 0
        original = session.task_state
        _MarkerAppendDenied(monkeypatch, session.store.path, times=1)
        _deny_truncate(monkeypatch)
        session.store._fh = _TaskReplacementFlushFailure(session.store._fh)  # type: ignore[assignment]
        client = _RecordingClient([_final("Must not run B.")])
        session.client = client
        code = session.run_turn("Task B: review the server.", task_relation="new_task")
        assert code == 1 and not client.requests
        assert session.task_state == original
        assert session.store.integrity_uncertain is False
        assert not session.store._uncommitted_sidecar_path.exists()
        disk = recover_task_state_from_events(
            read_session_events(session.store.path), session_id=session.store.session_id
        )
        memory = recover_task_state_from_events(
            session.store.events_snapshot(), session_id=session.store.session_id
        )
        assert disk.state == memory.state == original
        # The refused bytes are still in the file, behind the marker; the
        # error records the turn wrote afterwards are committed and read back.
        raw = session.store.path.read_text(encoding="utf-8")
        assert '"Task B: review the server."' in raw
        assert raw.index(f'"type": "{SESSION_STORE_UNCOMMITTED_EVENT}"') > raw.index(
            '"objective": "Task B: review the server."'
        )
        reasons = [item.get("reason") for item in _events(session.store.path, "error")]
        assert "task_acceptance_persist_failed" in reasons
        assert list(read_session_events(session.store.path)) == session.store.events_snapshot()
        # A later ordinary turn still works on task A.
        follow = _RecordingClient([_final("Continuing A.")])
        session.client = follow
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert session.task_state == original
    finally:
        session.close()
    resumed = _session(
        tmp_path, mode="readonly", session_id_override="third-review-failed-turn-target"
    )
    try:
        ok, message = _resume_into(resumed, "third-review-failed-turn")
        assert ok, message
        assert resumed.task_state.objective == original.objective
        assert resumed.task_state.task_id == original.task_id
    finally:
        resumed.close()


def test_h3_unknown_write_offset_does_not_erase_a_previously_committed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe: ``fstat`` fails and the attempt leaves malformed JSON."""

    store = _store(tmp_path)
    try:
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": _payload()})
        inner = store._fh

        class PartialWrite:
            def write(self, text: str) -> None:
                inner.write('{"type":"note","payload":')
                inner.flush()
                raise OSError("injected partial record failure")

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

        store._fh = PartialWrite()  # type: ignore[assignment]

        def failed_fstat(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("injected offset measurement failure")

        monkeypatch.setattr(stores.os, "fstat", failed_fstat)
        with pytest.raises(SessionStoreWriteError) as error:
            store.append("note", {"new": "not accepted"})
        assert error.value.outcome == "refused"
        assert _replay(store) == store.events_snapshot()
        assert _types(_replay(store)) == [TASK_STATE_EVENT]
        # The store measured the start through the handle/path instead, so
        # the partial bytes were truncated away and nothing is guessed.
        assert '"new"' not in store.path.read_text(encoding="utf-8")
        assert SESSION_STORE_UNCOMMITTED_EVENT not in store.path.read_text(encoding="utf-8")
        store.append("note", {"after": True})
        replay = _assert_disk_memory_reopen_agree(tmp_path, store)
        assert _types(replay) == [TASK_STATE_EVENT, "note"]
    finally:
        store.close()


def test_h3_unmeasurable_log_refuses_the_write_before_it_is_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When neither the handle nor the path can say how long the log is, the
    record is not written at all: nothing to disown, nothing to guess."""

    store = _store(tmp_path)
    try:
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": _payload()})
        before = store.path.read_bytes()

        def failed_fstat(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("injected offset measurement failure")

        real_stat = Path.stat

        def failed_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == store.path:
                raise OSError("injected size measurement failure")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(stores.os, "fstat", failed_fstat)
        monkeypatch.setattr(Path, "stat", failed_stat)
        inner = store._fh

        class NoTell:
            def tell(self) -> None:
                raise OSError("injected tell failure")

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

        store._fh = NoTell()  # type: ignore[assignment]
        with pytest.raises(SessionStoreWriteError) as error:
            store.append("note", {"never": "written"})
        assert error.value.outcome == "refused"
        assert "was not attempted" in str(error.value)
        monkeypatch.undo()
        assert store.path.read_bytes() == before
        assert _types(store.events_snapshot()) == [TASK_STATE_EVENT]
        store.append("note", {"after": True})
        replay = _assert_disk_memory_reopen_agree(tmp_path, store)
        assert _types(replay) == [TASK_STATE_EVENT, "note"]
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["complete", "partial"])
def test_h3_marker_refusal_survives_later_appends_reopen_and_new_task_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Sequence: refusal (marker path, complete or partial bytes) → ordinary
    append → task-state append → read → reopen → read → append. Earlier
    committed tasks are never erased, later valid records never lost, the
    refused record never resurrected."""

    store = _store(tmp_path)
    try:
        original = _payload()
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": original})
        _deny_truncate(monkeypatch)
        wrapper = _FlushErrorAfterCompleteWrite if failure == "complete" else _PartialWriteThenError
        store._fh = wrapper(store._fh)  # type: ignore[assignment]
        replacement = copy.deepcopy(original)
        replacement.update(objective="Task B.", sequence=2, task_id="s:task:2")
        with pytest.raises(SessionStoreWriteError) as error:
            store.append(TASK_STATE_EVENT, {"transition": "replaced", "state": replacement})
        assert error.value.outcome == "refused"
        assert store.integrity_uncertain is False
        raw = store.path.read_text(encoding="utf-8")
        assert f'"type": "{SESSION_STORE_UNCOMMITTED_EVENT}"' in raw
        assert _disk_task(store).state.objective == "Task A."
        store.append("warning", {"warning": "acceptance failed"})
        amended = SessionTaskState.from_payload(original)
        assert amended is not None
        amended = transition_task_state(
            amended,
            instruction="Keep the CLI flags.",
            relation="amendment",
            session_id="s",
            origin_event_id="s:9",
        ).state
        store.append(TASK_STATE_EVENT, {"transition": "amended", "state": amended.to_payload()})
        replay = _assert_disk_memory_reopen_agree(tmp_path, store)
        assert _types(replay) == [TASK_STATE_EVENT, "warning", TASK_STATE_EVENT]
        assert _disk_task(store).state == amended
        assert _disk_task(store).state.objective == "Task A."
        assert "Keep the CLI flags." in _disk_task(store).state.amendments
        assert _disk_task(store).state.sequence == 1
    finally:
        store.close()


def test_h3_sidecar_is_folded_into_a_marker_by_the_next_append_or_by_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker versus sidecar reconciliation, both ways."""

    # (a) Reconciled by the next append once the log takes the marker.
    store = _store(tmp_path)
    try:
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": _payload()})
        _deny_truncate(monkeypatch)
        guard = _MarkerAppendDenied(monkeypatch, store.path)
        store._fh = _FlushErrorAfterCompleteWrite(store._fh)  # type: ignore[assignment]
        with pytest.raises(SessionStoreWriteError) as error:
            store.append("note", {"phantom": 1})
        assert error.value.outcome == "refused" and store.integrity_uncertain
        assert store._uncommitted_sidecar_path.exists()
        assert _types(_replay(store)) == [TASK_STATE_EVENT]
        guard.active = False
        store.append("note", {"after": 1})
        assert not store.integrity_uncertain and not store._uncommitted_sidecar_path.exists()
        assert SESSION_STORE_UNCOMMITTED_EVENT in store.path.read_text(encoding="utf-8")
        replay = _assert_disk_memory_reopen_agree(tmp_path, store)
        assert _types(replay) == [TASK_STATE_EVENT, "note"]
        assert [event["payload"] for event in replay if event["type"] == "note"] == [{"after": 1}]
    finally:
        store.close()
    # (b) Reconciled by the next open when the process ended with a sidecar.
    other_dir = tmp_path / "b"
    other_dir.mkdir()
    store = SessionStore(
        enabled=True, sessions_dir=other_dir, session_id="s", cwd=str(other_dir), repo_root=None
    )
    try:
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": _payload()})
        guard = _MarkerAppendDenied(monkeypatch, store.path)
        store._fh = _FlushErrorAfterCompleteWrite(store._fh)  # type: ignore[assignment]
        with pytest.raises(SessionStoreWriteError):
            store.append("note", {"phantom": 2})
        assert store._uncommitted_sidecar_path.exists()
        assert _types(_replay(store)) == [TASK_STATE_EVENT]
    finally:
        store.close()  # the marker is still denied: the sidecar stays for the next open
    assert store._uncommitted_sidecar_path.exists()
    guard.active = False
    reopened = SessionStore(
        enabled=True, sessions_dir=other_dir, session_id="s", cwd=str(other_dir), repo_root=None
    )
    try:
        assert not reopened._uncommitted_sidecar_path.exists()
        assert not reopened.integrity_uncertain
        assert _types(reopened.events_snapshot()) == [TASK_STATE_EVENT]
        reopened.append("note", {"after": 2})
        assert _types(_replay(reopened)) == [TASK_STATE_EVENT, "note"]
        assert _replay(reopened) == reopened.events_snapshot()
    finally:
        reopened.close()


def test_h3_legacy_boundary_without_a_start_offset_refuses_recovery_explicitly(
    tmp_path: Path,
) -> None:
    """A marker from an earlier build that cannot say where the failed attempt
    began is never applied to a guessed record: the committed record before it
    is kept in the replay, and task recovery reports the unidentified boundary
    instead of adopting or discarding a task. A user /clear boundary resets."""

    log = tmp_path / "legacy.jsonl"
    accepted = {
        "type": TASK_STATE_EVENT,
        "session_id": "s",
        "payload": {"transition": "accepted", "relation": "auto", "state": _payload()},
    }
    marker = {
        "type": SESSION_STORE_UNCOMMITTED_EVENT,
        "session_id": "s",
        "payload": {"from_offset": None, "to_offset": None, "event_type": "note"},
    }
    cleared = {
        "type": TASK_STATE_EVENT,
        "session_id": "s",
        "payload": {"transition": "cleared", "relation": "host", "state": None},
    }
    boundary = {
        "type": "conversation_cleared",
        "session_id": "s",
        "payload": {"trigger": "user_command"},
    }
    later = {
        "type": TASK_STATE_EVENT,
        "session_id": "s",
        "payload": {"transition": "accepted", "relation": "auto", "state": _payload("Task C.", 3)},
    }
    log.write_text(
        "\n".join(json.dumps(item) for item in [accepted, marker]) + "\n", encoding="utf-8"
    )
    replay = list(read_session_events(log))
    assert _types(replay) == [TASK_STATE_EVENT, SESSION_STORE_UNCOMMITTED_EVENT]
    assert replay[1]["payload"]["unidentified"] is True
    recovered = recover_task_state_from_events(replay, session_id="s")
    assert recovered.recovery == "unrecoverable"
    assert recovered.reason == "uncommitted_record_unidentified"
    assert recovered.state is None
    # Later task events in the same stretch do not override the refusal…
    log.write_text(
        "\n".join(json.dumps(item) for item in [accepted, marker, later]) + "\n", encoding="utf-8"
    )
    recovered = recover_task_state_from_events(read_session_events(log), session_id="s")
    assert recovered.recovery == "unrecoverable"
    assert recovered.reason == "uncommitted_record_unidentified"
    # …but a user-driven clear boundary does, exactly as for any other record.
    log.write_text(
        "\n".join(json.dumps(item) for item in [accepted, marker, cleared, boundary, later]) + "\n",
        encoding="utf-8",
    )
    recovered = recover_task_state_from_events(read_session_events(log), session_id="s")
    assert recovered.recovery == "event" and recovered.state.objective == "Task C."
    # A located marker is applied silently, as before.
    body = json.dumps(accepted) + "\n"
    phantom = json.dumps({"type": "note", "payload": {"phantom": True}}) + "\n"
    located = {
        "type": SESSION_STORE_UNCOMMITTED_EVENT,
        "session_id": "s",
        "payload": {"from_offset": len(body.encode()), "to_offset": len((body + phantom).encode())},
    }
    log.write_text(body + phantom + json.dumps(located) + "\n" + json.dumps(later) + "\n")
    assert _types(list(read_session_events(log))) == [TASK_STATE_EVENT, TASK_STATE_EVENT]
    recovered = recover_task_state_from_events(read_session_events(log), session_id="s")
    assert recovered.recovery == "event" and recovered.state.objective == "Task C."


# ===========================================================================
# H1 — exact amendments and an enforceable delivery boundary
# ===========================================================================


_BLOCK_AMENDMENT = "The required output is exactly this block, including the repeated line:\nBEGIN\nKEEP\nKEEP\nEND"


def _unwrap(request: list[dict[str, Any]]) -> str:
    rendered = "\n".join(
        message["content"] for message in request if isinstance(message.get("content"), str)
    )
    return "\n".join(line[2:] if line.startswith("- ") else line for line in rendered.splitlines())


def test_h1_multiline_amendment_repetition_survives_retry_as_exact_content(tmp_path: Path) -> None:
    """Reviewer probe."""

    session = _session(tmp_path, mode="readonly")
    try:
        session.client = _RecordingClient([_final("Started.")])
        assert session.run_turn("Review the output formatter.") == 0
        session.client = _FailingClient()
        with pytest.raises(LLMError):
            session.run_turn(_BLOCK_AMENDMENT, task_relation="amendment")
        assert session.task_state.amendments == (_BLOCK_AMENDMENT,)
        assert "KEEP" in session.task_state.amendments
        retry = _RecordingClient([_final("Continuing.")])
        session.client = retry
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert "BEGIN\nKEEP\nKEEP\nEND" in _unwrap(retry.requests[0])
        # Exact block, pinned, and the amendment's own message not duplicated.
        requirements = _single_requirements(retry.requests[0])
        assert f"<accepted_amendment>\n{_BLOCK_AMENDMENT}\n</accepted_amendment>" in requirements
        assert "delivery: complete" in requirements
        assert _BLOCK_AMENDMENT not in _user_texts(retry.requests[0])
        brief = _single_brief(retry.requests[0])
        assert "- BEGIN\n- KEEP\n- KEEP\n- END" in brief
    finally:
        session.close()


def test_h1_amendments_with_blank_lines_indentation_and_crlf_are_exact_everywhere(
    tmp_path: Path,
) -> None:
    text = 'Expected output:\r\n\r\n    {\r\n      "a":  1\r\n    }\r\n\r\nEND\nEND'
    session = _session(tmp_path, mode="readonly")
    try:
        session.client = _RecordingClient([_final("ok")])
        assert session.run_turn("Review the formatter.") == 0
        client = _RecordingClient([_final("ok")])
        session.client = client
        assert session.run_turn(text, task_relation="amendment") == 0
        state = session.task_state
        assert state.amendments == (text,)
        assert isinstance(state.amendments, AcceptedAmendments)
        assert state.amendments.lines() == (
            ("Expected output:", "    {", '      "a":  1', "    }", "END", "END"),
        )
        # Persisted, replayed and rendered exactly.
        payload = _events(session.store.path, TASK_STATE_EVENT)[-1]["state"]
        assert payload["amendments"] == [text]
        replayed = recover_task_state_from_events(
            read_session_events(session.store.path), session_id=session.store.session_id
        )
        assert replayed.state == state
        requirements = _single_requirements(client.requests[0])
        assert f"<accepted_amendment>\n{text}\n</accepted_amendment>" in requirements
        brief = _single_brief(client.requests[0])
        # Exact: a CRLF "blank" line still carries its "\r".
        assert "- Expected output:\r\n- \r\n-     {\r\n" in brief
        # Whole-unit repeat folds; a single line of it offered again does not.
        again = transition_task_state(
            state, instruction=text, relation="amendment", session_id="s", origin_event_id="x"
        )
        assert again.kind == "kept" and again.reason == "no_new_constraint"
        one_line = transition_task_state(
            state, instruction="END", relation="amendment", session_id="s", origin_event_id="y"
        )
        assert one_line.kind == "amended" and one_line.state.amendments == ("END", text)
    finally:
        session.close()


def test_h1_exact_amendment_block_survives_rollover_and_resume(tmp_path: Path) -> None:
    session = _session(tmp_path, mode="readonly", session_id_override="third-review-block-source")
    try:
        session.client = _RecordingClient([_final("ok")])
        assert session.run_turn("Review the output formatter.") == 0
        session.client = _RecordingClient([_final("ok")])
        assert session.run_turn(_BLOCK_AMENDMENT, task_relation="amendment") == 0
        _rollover(session, tmp_path)
        assert session.task_state.amendments == (_BLOCK_AMENDMENT,)
        client = _RecordingClient([_final("ok")])
        session.client = client
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert "BEGIN\nKEEP\nKEEP\nEND" in _unwrap(client.requests[0])
        assert f"<accepted_amendment>\n{_BLOCK_AMENDMENT}\n</accepted_amendment>" in (
            _single_requirements(client.requests[0])
        )
    finally:
        session.close()
    resumed = _session(tmp_path, mode="readonly", session_id_override="third-review-block-target")
    try:
        ok, message = _resume_into(resumed, "third-review-block-source")
        assert ok, message
        assert resumed.task_state.amendments == (_BLOCK_AMENDMENT,)
        client = _RecordingClient([_final("ok")])
        resumed.client = client
        assert resumed.run_turn("continue", task_relation="continuation") == 0
        assert f"<accepted_amendment>\n{_BLOCK_AMENDMENT}\n</accepted_amendment>" in (
            _single_requirements(client.requests[0])
        )
    finally:
        resumed.close()


def _over_budget_objective(context_repeats: int) -> str:
    return (
        "Inspect this fixture, then update the result.\n"
        + "Context. " * context_repeats
        + "\nMANDATORY: The result must include the exact token KEEP_POLICY_V1."
    )


@pytest.mark.parametrize("context_repeats", [12, 3200])
def test_h1_over_budget_rollover_does_not_mutate_with_missing_accepted_requirements(
    tmp_path: Path, context_repeats: int
) -> None:
    """Reviewer probe (12 = under-budget control, 3200 = over budget), with the
    host's explicit needs-input condition asserted for the over-budget case."""

    objective = _over_budget_objective(context_repeats)
    critical = "KEEP_POLICY_V1"
    session = _session(tmp_path, mode="auto")
    try:
        session.client = _RecordingClient([_final("Starting the inspection.")])
        assert session.run_turn(objective) == 0
        original_state = session.task_state
        _rollover(session, tmp_path)
        assert session.task_state == original_state
        client = _RecordingClient(
            [
                _tool(
                    "write_after_rollover",
                    "fs_write",
                    {"path": "result.txt", "content": "generated\n"},
                ),
                _final("Updated the result."),
            ]
        )
        session.client = client
        code = session.run_turn("continue", task_relation="continuation")
        delivered = bool(client.requests) and critical in json.dumps(client.requests[0])
        mutated = (tmp_path / "result.txt").exists()
        assert delivered or not mutated
        if len(objective) <= _TASK_REQUIREMENTS_MAX_CHARS:
            assert code == 0 and delivered and mutated
            return
        # Over budget: the exact requirements are in neither the pinned
        # messages nor the transcript, so the host refuses before dispatch.
        assert code == 1 and not client.requests and not mutated
        errors = _events(session.store.path, "error")
        assert errors and errors[-1]["reason"] == "task_requirements_unavailable"
        assert errors[-1]["missing"] == ["accepted request"]
        assert errors[-1]["stage"] == "before_first_request"
        assert "Restate the missing requirements" in errors[-1]["error"]
        assert session.task_state == original_state
        assert "delivery: partial" in _session_task_requirements_content(session)
        # The transcript is restored as after a persistence failure: no
        # dangling "continue".
        assert _user_texts(session.messages)[-1:] != ["continue"]
        # Restating the requirement (as an amendment) makes the task actionable
        # again without a new task: the amendment is pinned and complete.
        restated = _RecordingClient(
            [
                _tool("write_after_restate", "fs_write", {"path": "result.txt", "content": "x\n"}),
                _final("Updated the result."),
            ]
        )
        session.client = restated
        code = session.run_turn(
            "MANDATORY: The result must include the exact token KEEP_POLICY_V1.",
            task_relation="amendment",
        )
        # The request itself is still beyond the budget, so the turn stays
        # refused until the request is restated or replaced…
        assert code == 1 and not restated.requests
        # …and an explicit new task of deliverable size proceeds.
        fresh = _RecordingClient(
            [
                _tool("write_new_task", "fs_write", {"path": "result.txt", "content": "x\n"}),
                _final("Updated the result."),
            ]
        )
        session.client = fresh
        assert (
            session.run_turn(
                "Update result.txt; it must include the exact token KEEP_POLICY_V1.",
                task_relation="new_task",
            )
            == 0
        )
        assert critical in json.dumps(fresh.requests[0])
        assert (tmp_path / "result.txt").exists()
    finally:
        session.close()


def test_h1_over_budget_request_is_refused_after_compaction_and_after_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compaction boundary (mid-turn, before the model acts on the summary's
    version of the task) and resume boundary (the snapshot no longer holds the
    original message)."""

    monkeypatch.setenv("ALYSIS_MODEL_COMPACTOR", "compactor-model")
    objective = _over_budget_objective(3200)
    session = create_session(
        cfg=_compaction_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / ".sessions",
        session_id_override="third-review-compaction-source",
        enable_compaction=True,
        verification_enabled=False,
    )
    try:
        summary = {
            "goal": "Update the result",
            "constraints": [],
            "decisions": [],
            "work_done": [],
            "open_threads": [],
            "next_steps": [],
        }
        session.conversation_compactor.compactor_client = _RecordingClient(
            [_final(json.dumps(summary))]
        )
        session.client = _RecordingClient([_final("Starting.")])
        assert session.run_turn(objective) == 0
        original_state = session.task_state
        for n in range(3):
            session.messages.append({"role": "user", "content": f"filler {n} " + "l" * 9000})
            session.messages.append({"role": "assistant", "content": "ack " + "m" * 9000})
        client = _RecordingClient(
            [
                _tool(
                    "write_after_compaction", "fs_write", {"path": "result.txt", "content": "x\n"}
                ),
                _final("Updated the result."),
            ]
        )
        session.client = client
        code = session.run_turn("continue", task_relation="continuation")
        events = list(read_session_events(session.store.path))
        assert any(event["type"] == "conversation_summary_updated" for event in events)
        delivered = bool(client.requests) and "KEEP_POLICY_V1" in json.dumps(client.requests[0])
        assert delivered or not (tmp_path / "result.txt").exists()
        assert code == 1 and not client.requests
        errors = _events(session.store.path, "error")
        assert errors[-1]["reason"] == "task_requirements_unavailable"
        assert errors[-1]["stage"] == "after_compaction"
        assert session.task_state == original_state
    finally:
        session.close()
    resumed = _session(tmp_path, mode="auto", session_id_override="third-review-compaction-target")
    try:
        ok, message = _resume_into(resumed, "third-review-compaction-source")
        assert ok, message
        assert resumed.task_state == original_state
        assert undelivered_task_requirements(resumed.messages, resumed.task_state) == [
            "accepted request"
        ]
        client = _RecordingClient(
            [
                _tool("write_after_resume", "fs_write", {"path": "result.txt", "content": "x\n"}),
                _final("Updated the result."),
            ]
        )
        resumed.client = client
        assert resumed.run_turn("continue", task_relation="continuation") == 1
        assert not client.requests and not (tmp_path / "result.txt").exists()
        assert _events(resumed.store.path, "error")[-1]["reason"] == "task_requirements_unavailable"
    finally:
        resumed.close()


def test_h1_over_budget_request_proceeds_while_its_original_message_is_present(
    tmp_path: Path,
) -> None:
    """A partial pinned projection is not a problem while the exact accepted
    text is still in the outgoing context: the accepting turn, an ordinary
    follow-up, and the provider-retry rehydration all proceed."""

    objective = _over_budget_objective(3200)
    session = _session(tmp_path, mode="auto")
    try:
        client = _RecordingClient(
            [
                _tool("write_first", "fs_write", {"path": "result.txt", "content": "x\n"}),
                _final("Updated the result."),
            ]
        )
        session.client = client
        assert session.run_turn(objective) == 0
        assert "KEEP_POLICY_V1" in json.dumps(client.requests[0])
        assert (tmp_path / "result.txt").exists()
        assert undelivered_task_requirements(session.messages, session.task_state) == []
        follow = _RecordingClient([_final("Still here.")])
        session.client = follow
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert "KEEP_POLICY_V1" in json.dumps(follow.requests[0])
        session.client = _FailingClient()
        with pytest.raises(LLMError):
            session.run_turn("Also note this.", task_relation="amendment")
        retry = _RecordingClient([_final("Recovered.")])
        session.client = retry
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert "KEEP_POLICY_V1" in json.dumps(retry.requests[0])
    finally:
        session.close()


def test_h1_delivery_check_reads_the_actual_context_not_the_state_size() -> None:
    state = SessionTaskState(
        task_id="s:task:1",
        objective="x" * (_TASK_REQUIREMENTS_MAX_CHARS + 10),
        session_id="s",
        sequence=1,
        amendments=(_BLOCK_AMENDMENT, "Short one."),
    )
    brief = {"role": "user", "content": "<task_brief>\n...\n</task_brief>\n"}
    original = {"role": "user", "content": state.objective}
    block = {"role": "user", "content": _BLOCK_AMENDMENT}
    short = {"role": "user", "content": "Short one."}
    assert undelivered_task_requirements([brief], state) == [
        "accepted request",
        "accepted amendment 1",
        "accepted amendment 2",
    ]
    assert undelivered_task_requirements([brief, original, block, short], state) == []
    assert undelivered_task_requirements([brief, original, short], state) == [
        "accepted amendment 1"
    ]
    # A pinned requirements message that delivers the amendments but only the
    # head of the request leaves exactly the request missing.
    from alysis_code.agent.prompt_context import _render_task_requirements_from_state

    pinned = {"role": "user", "content": _render_task_requirements_from_state(state)}
    assert undelivered_task_requirements([brief, pinned], state) == ["accepted request"]
    assert undelivered_task_requirements([brief, pinned, original], state) == []
    assert undelivered_task_requirements([brief], None) == []


# ===========================================================================
# H2 — missing and explicitly null are different
# ===========================================================================


def test_h2_null_amendments_are_invalid_not_an_empty_constraint_set() -> None:
    """Reviewer probe."""

    payload = _payload()
    payload["amendments"] = None
    recovered = recover_task_state_from_events(
        [
            {
                "type": TASK_STATE_EVENT,
                "session_id": "s",
                "payload": {"transition": "accepted", "state": payload},
            }
        ],
        session_id="s",
    )
    assert recovered.state is None and recovered.recovery == "unrecoverable"
    assert recovered.reason == "latest_task_state_event_invalid:state_amendments_invalid"


@pytest.mark.parametrize("field", ["amendments", "prior_objectives"])
def test_h2_list_fields_missing_empty_and_null_are_distinguished(field: str) -> None:
    payload = _payload()
    del payload[field]
    state, why = validate_task_state_payload(payload)
    assert why == "" and state is not None and getattr(state, field) == ()
    payload[field] = []
    state, why = validate_task_state_payload(payload)
    assert why == "" and state is not None and getattr(state, field) == ()
    for bad in (None, "text", 3, {"a": 1}, [None], ["ok", 2], [""]):
        payload[field] = bad
        state, why = validate_task_state_payload(payload)
        assert state is None and why == f"state_{field}_invalid", bad
