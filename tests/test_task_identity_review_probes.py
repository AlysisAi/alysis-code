"""Independent-review probes for host-owned task identity (R1–R5).

Reconstructed from the independent review of commit ``8be3151b``: fifteen
deterministic invariant probes in five groups. They use real production
sessions, the real JSONL session store (persistence failures are injected at
the file handle, before any record bytes are written), scripted model clients
that snapshot every request at call time, and the actual chat command,
resume and turn paths.

R1 persistence consistency, R2 lossless accepted requirements, R3 validated
recovery outcomes, R4 non-task input provenance, R5 path-first accepted
requests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.prompt_context import (
    _TASK_BRIEF_EMPTY_STATUS,
    _TASK_BRIEF_UNRECOVERED_STATUS,
    _session_task_brief_content,
)
from alysis_code.agent.task_state import (
    TASK_STATE_EVENT,
    TaskPersistenceError,
    recover_task_state_from_events,
)
from alysis_code.cli_impl.chat import loop as chat_loop_mod
from alysis_code.cli_impl.chat.state import _ChatExecutionRequest
from alysis_code.llm.openai_compat import LLMError
from alysis_code.session_store import read_session_events
from tests.test_task_identity_hardening import (
    _assert_objective_brief,
    _dispatch_command,
    _events,
    _FailingClient,
    _final,
    _RecordingClient,
    _resume_into,
    _run_chat_request,
    _session,
    _single_brief,
    _single_requirements,
    _transitions,
    _user_texts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RejectingHandle:
    """Proxy for the store's real JSONL file handle.

    Refuses task-state records *before any bytes are written* (the failure
    mode of a full disk or a closed descriptor) and passes everything else
    through unchanged, so the surrounding events really reach the file.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.rejected: list[str] = []

    def write(self, text: str) -> int:
        if f'"type": "{TASK_STATE_EVENT}"' in text:
            self.rejected.append(text)
            raise OSError(28, "No space left on device")
        return self._inner.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _reject_task_state_writes(session: Any) -> _RejectingHandle:
    store = session.store
    assert store.enabled and store._fh is not None, "the session must own a real log"
    proxy = _RejectingHandle(store._fh)
    store._fh = proxy  # type: ignore[assignment]
    return proxy


def _restore_handle(session: Any, proxy: _RejectingHandle) -> None:
    if session.store._fh is proxy:
        session.store._fh = proxy._inner


def _disk_recovery(session: Any) -> Any:
    return recover_task_state_from_events(
        read_session_events(session.store.path), session_id=session.store.session_id
    )


def _snapshot_recovery(session: Any) -> Any:
    return recover_task_state_from_events(
        session.store.events_snapshot(), session_id=session.store.session_id
    )


def _write_log(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _valid_state_event(
    session_id: str, objective: str = "Task A: refactor the parser."
) -> dict[str, Any]:
    return {
        "type": TASK_STATE_EVENT,
        "session_id": session_id,
        "payload": {
            "transition": "accepted",
            "relation": "auto",
            "state": {
                "schema_version": 1,
                "task_id": f"{session_id}:task:1",
                "objective": objective,
                "session_id": session_id,
                "sequence": 1,
                "origin": "user_instruction",
                "origin_event_id": f"{session_id}:2",
                "accepted_at": "",
                "amendments": [],
                "prior_objectives": [],
            },
        },
    }


# ---------------------------------------------------------------------------
# R1 — persistence failures must not split authoritative state
# ---------------------------------------------------------------------------


def test_r1_failed_replacement_leaves_no_phantom_event_in_snapshot_or_disk(tmp_path: Path) -> None:
    session = _session(tmp_path, mode="auto")
    try:
        session.client = _RecordingClient([_final("A done.")])
        assert session.run_turn("Task A: add logging.") == 0
        task_a = session.task_state
        proxy = _reject_task_state_writes(session)
        client = _RecordingClient([_final("must not run")])
        session.client = client
        exit_code = session.run_turn("Task B: migrate to click.", task_relation="new_task")
        _restore_handle(session, proxy)
        assert exit_code == 1
        assert client.requests == []
        assert proxy.rejected, "the task-state write was never attempted at the file"
        assert session.task_state == task_a
        assert _disk_recovery(session).state == task_a
        # The store's own event view must agree with the disk: no phantom B.
        assert _snapshot_recovery(session).state == task_a
        assert _transitions(session.store.path) == ["accepted"]
        _assert_objective_brief(_session_task_brief_content(session), "Task A: add logging.")
    finally:
        session.close()


def test_r1_failed_initial_acceptance_is_not_resurrected_as_a_legacy_task_on_resume(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, mode="auto", session_id_override="failed-accept")
    try:
        proxy = _reject_task_state_writes(session)
        client = _RecordingClient([_final("must not run")])
        session.client = client
        assert session.run_turn("Implement the export command.") == 1
        _restore_handle(session, proxy)
        assert client.requests == []
        assert session.task_state is None
        assert _snapshot_recovery(session).state is None
        assert _disk_recovery(session).state is None
    finally:
        session.close()

    fresh = _session(tmp_path, mode="auto", session_id_override="failed-accept-fresh")
    try:
        ok, message = _resume_into(fresh, "failed-accept")
        assert ok is True, message
        assert fresh.task_state is None, "a request that was never accepted came back as a task"
        recovered = _disk_recovery(fresh)
        assert recovered.state is None
        assert recovered.recovery != "legacy_user_events"
        assert _TASK_BRIEF_EMPTY_STATUS in _single_brief(fresh.messages)
    finally:
        fresh.close()


def test_r1_failed_clear_keeps_live_state_disk_and_prompt_in_agreement(tmp_path: Path) -> None:
    session = _session(tmp_path, mode="auto")
    try:
        session.client = _RecordingClient([_final("A done.")])
        assert session.run_turn("Task A: add logging.") == 0
        task_a = session.task_state
        proxy = _reject_task_state_writes(session)
        with pytest.raises(TaskPersistenceError):
            chat_loop_mod._clear_chat_conversation(session=session, pending_images=[])
        _restore_handle(session, proxy)
        # Nothing was retired anywhere: live, disk, snapshot and prompt agree on A.
        assert session.task_state == task_a
        assert _disk_recovery(session).state == task_a
        assert _snapshot_recovery(session).state == task_a
        brief = _session_task_brief_content(session)
        _assert_objective_brief(brief, "Task A: add logging.")
        assert _TASK_BRIEF_EMPTY_STATUS not in brief
        assert not [
            item
            for item in _events(session.store.path, "conversation_cleared")
            if item.get("trigger") == "user_command"
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# R2 — accepted requirements are never silently discarded
# ---------------------------------------------------------------------------


def _long_request(total_chars: int) -> str:
    head = "Implement the ingestion pipeline for the new billing exports.\n"
    filler_line = "Detail: keep every field name exactly as documented in the vendor spec.\n"
    tail = "MANDATORY: reject any record whose checksum does not match."
    body = head
    while len(body) + len(tail) < total_chars:
        body += filler_line
    return body + tail


def test_r2_long_request_is_stored_and_recovered_without_clipping(tmp_path: Path) -> None:
    request = _long_request(5293)
    assert len(request) >= 5293
    session = _session(tmp_path, mode="auto", session_id_override="long-request")
    try:
        session.client = _RecordingClient([_final("ok")])
        assert session.run_turn(request) == 0
        state = session.task_state
        assert state is not None
        assert state.objective == request
        assert state.objective.endswith(
            "MANDATORY: reject any record whose checksum does not match."
        )
        persisted = _events(session.store.path, TASK_STATE_EVENT)[-1]["state"]["objective"]
        assert persisted == request
    finally:
        session.close()
    fresh = _session(tmp_path, mode="auto", session_id_override="long-request-fresh")
    try:
        ok, _ = _resume_into(fresh, "long-request")
        assert ok is True
        assert fresh.task_state.objective == request
    finally:
        fresh.close()


def test_r2_fourth_amendment_does_not_evict_the_first_active_constraint(tmp_path: Path) -> None:
    constraints = [
        "Preserve backwards-compatible command flags.",
        "Keep the exit codes documented in README.",
        "Do not add new runtime dependencies.",
        "Log every rejected record at WARNING.",
    ]
    session = _session(tmp_path, mode="auto")
    try:
        session.client = _RecordingClient([_final("ok")])
        assert session.run_turn("Migrate the CLI entry points to click.") == 0
        for constraint in constraints:
            session.client = _RecordingClient([_final("noted")])
            assert session.run_turn(constraint, task_relation="amendment") == 0
        state = session.task_state
        assert set(state.amendments) == set(constraints)
        assert "Preserve backwards-compatible command flags." in state.amendments
        client = _RecordingClient([_final("continuing")])
        session.client = client
        assert session.run_turn("continue") == 0
        brief = _single_brief(client.requests[0])
        for constraint in constraints:
            assert f"- {constraint}" in brief
        persisted = _events(session.store.path, TASK_STATE_EVENT)[-1]["state"]["amendments"]
        assert set(persisted) == set(constraints)
    finally:
        session.close()


def test_r2_full_request_survives_provider_failure_and_continue(tmp_path: Path) -> None:
    request = (
        "Refactor the record exporter so it streams rows instead of buffering the whole file, "
        "keep the CSV column order identical to the current output, add a regression test that "
        "compares a 10k-row export byte for byte, keep the existing command-line flags working, "
        "and document the memory ceiling in README. "
        "Never change the public export function parse_record."
    )
    # Well below any state limit, but longer than the old per-line brief clip.
    assert 300 <= len(request) <= 500, len(request)
    session = _session(tmp_path, mode="auto")
    try:
        session.client = _FailingClient()
        with pytest.raises(LLMError):
            session.run_turn(request)
        assert session.task_state.objective == request
        client = _RecordingClient([_final("continuing")])
        session.client = client
        assert session.run_turn("continue") == 0
        first_request = json.dumps(client.requests[0])
        assert "Never change the public export function parse_record." in first_request
        assert "continue" in _user_texts(client.requests[0])
    finally:
        session.close()


# ---------------------------------------------------------------------------
# R3 — malformed or unknown recovery input never becomes valid task state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latest_payload",
    [None, [], {}, {"transition": "accepted", "relation": "auto"}],
    ids=["payload_null", "payload_list", "payload_empty_object", "payload_missing_state"],
)
def test_r3_malformed_latest_task_state_event_is_unrecoverable_not_stale_or_clear(
    latest_payload: Any,
) -> None:
    events = [
        {"type": "session_start", "session_id": "s", "payload": {"mode": "auto"}},
        {
            "type": "user_message",
            "session_id": "s",
            "payload": {"content": "Task A: refactor the parser."},
        },
        _valid_state_event("s"),
        {"type": TASK_STATE_EVENT, "session_id": "s", "payload": latest_payload},
    ]
    recovered = recover_task_state_from_events(events, session_id="s")
    assert recovered.state is None
    assert recovered.recovery == "unrecoverable", recovered


def test_r3_unsupported_schema_version_is_unrecoverable() -> None:
    event = _valid_state_event("s")
    event["payload"]["state"]["schema_version"] = 999
    recovered = recover_task_state_from_events(
        [{"type": "session_start", "session_id": "s", "payload": {}}, event], session_id="s"
    )
    assert recovered.state is None
    assert recovered.recovery == "unrecoverable"


def test_r3_persisted_unrecoverable_outcome_survives_a_second_recovery(tmp_path: Path) -> None:
    sessions_dir = tmp_path / ".sessions"
    _write_log(
        sessions_dir / "broken-src.jsonl",
        [
            {"type": "session_start", "session_id": "broken-src", "payload": {"mode": "auto"}},
            {
                "type": "user_message",
                "session_id": "broken-src",
                "payload": {"content": "Refactor the exporter."},
            },
            {
                "type": TASK_STATE_EVENT,
                "session_id": "broken-src",
                "payload": {"state": {"task_id": "x"}},
            },
        ],
    )
    first = _session(tmp_path, mode="auto", session_id_override="broken-first")
    try:
        ok, _ = _resume_into(first, "broken-src")
        assert ok is True
        assert first.task_state is None and first.task_state_unrecovered is True
    finally:
        first.close()
    # The resumed session persisted its (unrecoverable) restore into the same log.
    assert _transitions(sessions_dir / "broken-src.jsonl")[-1].startswith("restore")
    second = _session(tmp_path, mode="auto", session_id_override="broken-second")
    try:
        ok, _ = _resume_into(second, "broken-src")
        assert ok is True
        assert second.task_state is None
        assert second.task_state_unrecovered is True, (
            "the limitation was lost on the second recovery"
        )
        assert _TASK_BRIEF_UNRECOVERED_STATUS in _single_brief(second.messages)
        recovered = _disk_recovery(second)
        assert recovered.recovery == "unrecoverable"
    finally:
        second.close()


def test_r3_continuation_in_an_unrecoverable_session_does_not_invent_a_task(tmp_path: Path) -> None:
    sessions_dir = tmp_path / ".sessions"
    _write_log(
        sessions_dir / "unrec-src.jsonl",
        [
            {"type": "session_start", "session_id": "unrec-src", "payload": {"mode": "auto"}},
            {
                "type": TASK_STATE_EVENT,
                "session_id": "unrec-src",
                "payload": {"state": {"task_id": "x"}},
            },
        ],
    )
    current = _session(tmp_path, mode="auto", session_id_override="unrec-current")
    try:
        ok, _ = _resume_into(current, "unrec-src")
        assert ok is True
        assert current.task_state_unrecovered is True
        for relation in ("continuation", None):
            client = _RecordingClient([_final("ok")])
            current.client = client
            kwargs = {"task_relation": relation} if relation else {}
            assert current.run_turn("continue", **kwargs) == 0
            # The message is delivered, but no objective is invented from it.
            assert "continue" in _user_texts(client.requests[0])
            assert current.task_state is None
            assert current.task_state_unrecovered is True
            assert _TASK_BRIEF_UNRECOVERED_STATUS in _single_brief(client.requests[0])
        client = _RecordingClient([_final("started")])
        current.client = client
        assert current.run_turn("Rewrite the exporter.", task_relation="new_task") == 0
        assert current.task_state.objective == "Rewrite the exporter."
        assert current.task_state_unrecovered is False
        _assert_objective_brief(_single_brief(client.requests[0]), "Rewrite the exporter.")
    finally:
        current.close()


# ---------------------------------------------------------------------------
# R4 — explicitly conversational input never becomes a task after resume
# ---------------------------------------------------------------------------


def test_r4_chat_only_input_is_not_promoted_to_a_task_by_resume(tmp_path: Path) -> None:
    session = _session(tmp_path, mode="auto", session_id_override="chat-only-src")
    try:
        session.client = _RecordingClient([_final("Hello!")])
        assert session.run_turn("Hello there.", chat_only=True) == 0
        assert session.task_state is None
    finally:
        session.close()
    fresh = _session(tmp_path, mode="auto", session_id_override="chat-only-fresh")
    try:
        ok, message = _resume_into(fresh, "chat-only-src")
        assert ok is True, message
        assert fresh.task_state is None, fresh.task_state
        recovered = _disk_recovery(fresh)
        assert recovered.state is None
        assert recovered.recovery != "legacy_user_events"
        assert _TASK_BRIEF_EMPTY_STATUS in _single_brief(fresh.messages)
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# R5 — path-first explicit requests establish identity before dispatch
# ---------------------------------------------------------------------------


PATH_FIRST_REQUEST = "/work/app.py: review the input validation."


def test_r5_objective_new_with_a_path_first_request_sets_the_task_before_dispatch(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, mode="auto")
    try:
        result, output = _dispatch_command(
            f"/objective new {PATH_FIRST_REQUEST}", session=session, tmp_path=tmp_path
        )
        assert isinstance(result, _ChatExecutionRequest), output
        assert result.task_relation == "new_task"
        assert result.instruction == PATH_FIRST_REQUEST
        client = _RecordingClient([_final("Reviewed.")])
        session.client = client
        assert _run_chat_request(session, result) == 0
        assert session.task_state is not None, "the path-first request was not accepted"
        assert session.task_state.objective == PATH_FIRST_REQUEST
        brief = _single_brief(client.requests[0])
        _assert_objective_brief(brief, PATH_FIRST_REQUEST)
        assert _TASK_BRIEF_EMPTY_STATUS not in brief
    finally:
        session.close()


def test_r5_path_first_request_through_run_turn(tmp_path: Path) -> None:
    # one-shot style: the accepted instruction itself starts with a path
    session = _session(tmp_path, mode="auto")
    try:
        client = _RecordingClient([_final("Reviewed.")])
        session.client = client
        assert session.run_turn(PATH_FIRST_REQUEST, task_relation="new_task") == 0
        assert session.task_state.objective == PATH_FIRST_REQUEST
        _assert_objective_brief(_single_brief(client.requests[0]), PATH_FIRST_REQUEST)
    finally:
        session.close()


def test_r2_oversized_request_is_delivered_exactly_after_provider_failure(tmp_path: Path) -> None:
    """Beyond the brief's projection budget the pinned ``<task_requirements>``
    message (rendered from host state, so it survives the provider-failure
    rollback) carries the complete accepted request; the first request after
    "continue" still carries the final requirement verbatim, and the transcript
    copy is not duplicated for it."""

    from alysis_code.agent.prompt_context import _TASK_BRIEF_OBJECTIVE_MAX_CHARS

    request = _long_request(_TASK_BRIEF_OBJECTIVE_MAX_CHARS + 1500)
    session = _session(tmp_path, mode="auto")
    try:
        session.client = _FailingClient()
        with pytest.raises(LLMError):
            session.run_turn(request)
        assert session.task_state.objective == request
        client = _RecordingClient([_final("continuing")])
        session.client = client
        assert session.run_turn("continue") == 0
        first_request = client.requests[0]
        assert _user_texts(first_request) == [
            "Repo summary: (no top-level files found)\n",
            "continue",
        ]
        requirements = _single_requirements(first_request)
        assert f"<accepted_request>\n{request}\n</accepted_request>" in requirements
        assert "delivery: complete" in requirements
        brief = _single_brief(first_request)
        assert "[accepted request continues:" in brief
        assert len(brief) < len(request)
        # The requirements message is pinned: it sits inside the prefix the
        # compactor protects, directly after the brief.
        brief_index = next(
            index
            for index, message in enumerate(session.messages)
            if str(message.get("content") or "").startswith("<task_brief>")
        )
        assert str(session.messages[brief_index + 1]["content"]).startswith("<task_requirements>")
        assert brief_index + 1 < session.pinned_prefix_len
    finally:
        session.close()


def test_r2_request_beyond_the_delivery_budget_is_rehydrated_after_provider_failure(
    tmp_path: Path,
) -> None:
    """When even the requirements message cannot carry the request, the
    transcript copy is the only complete channel: it is kept through the
    rollback, and the requirements message states the bounded clarification
    condition instead of pretending to be complete."""

    from alysis_code.agent.prompt_context import _TASK_REQUIREMENTS_MAX_CHARS

    request = _long_request(_TASK_REQUIREMENTS_MAX_CHARS + 2000)
    session = _session(tmp_path, mode="auto")
    try:
        session.client = _FailingClient()
        with pytest.raises(LLMError):
            session.run_turn(request)
        assert session.task_state.objective == request
        client = _RecordingClient([_final("continuing")])
        session.client = client
        assert session.run_turn("continue") == 0
        first_request = client.requests[0]
        assert request in _user_texts(first_request)
        assert _user_texts(first_request)[-1] == "continue"
        requirements = _single_requirements(first_request)
        assert "delivery: partial" in requirements
        missing = len(request) - _TASK_REQUIREMENTS_MAX_CHARS
        assert (
            f"[accepted request continues: {missing} more characters beyond the host delivery "
            f"budget of {_TASK_REQUIREMENTS_MAX_CHARS} characters;" in requirements
        )
        assert "Do not assume requirements you cannot see" in requirements
        assert request[:_TASK_REQUIREMENTS_MAX_CHARS] in requirements
        assert "MANDATORY: reject any record whose checksum does not match." not in requirements
    finally:
        session.close()


# ---------------------------------------------------------------------------
# R1 — the store boundary itself
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, *, enabled: bool = True, session_id: str = "store-probe") -> Any:
    import os

    from alysis_code.session_store import SessionStore

    return SessionStore(
        enabled=enabled,
        sessions_dir=tmp_path / "sessions",
        session_id=session_id,
        cwd=os.fspath(tmp_path),
        repo_root=None,
    )


def test_r1_store_publishes_a_record_only_after_it_reached_the_log(tmp_path: Path) -> None:
    from alysis_code.session_store import SessionStoreWriteError

    store = _store(tmp_path)
    try:
        first = store.append("user_message", {"content": "one"})
        size_after_first = store.path.stat().st_size
        proxy = _RejectingHandle(store._fh)
        store._fh = proxy  # type: ignore[assignment]
        with pytest.raises(SessionStoreWriteError):
            store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": None})
        assert [event["type"] for event in store.events_snapshot()] == ["user_message"]
        assert [event["type"] for event in store.events_since(0)[0]] == ["user_message"]
        assert store.path.stat().st_size == size_after_first
        assert [event["type"] for event in read_session_events(store.path)] == ["user_message"]
        # The store recovers on its own: the next record reopens the log and
        # continues the id sequence without a gap.
        second = store.append("assistant_message", {"content": "two"})
        assert first == "store-probe:1" and second == "store-probe:2"
        assert [event["type"] for event in read_session_events(store.path)] == [
            "user_message",
            "assistant_message",
        ]
    finally:
        store.close()


def test_r1_store_discards_a_record_whose_flush_failed(tmp_path: Path) -> None:
    from alysis_code.session_store import SessionStoreWriteError

    store = _store(tmp_path, session_id="flush-probe")
    try:
        store.append("user_message", {"content": "one"})
        size_before = store.path.stat().st_size

        class _FlushFails:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def flush(self) -> None:
                raise OSError(5, "Input/output error")

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        store._fh = _FlushFails(store._fh)  # type: ignore[assignment]
        with pytest.raises(SessionStoreWriteError):
            store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": None})
        # Bytes that may have been buffered are not left behind on disk, and
        # nothing was published.
        assert store.path.stat().st_size == size_before
        assert [event["type"] for event in store.events_snapshot()] == ["user_message"]
        assert [event["type"] for event in read_session_events(store.path)] == ["user_message"]
        store.append("assistant_message", {"content": "two"})
        assert len(list(read_session_events(store.path))) == 2
    finally:
        store.close()


def test_r1_no_log_store_keeps_deliberate_in_memory_operation(tmp_path: Path) -> None:
    store = _store(tmp_path, enabled=False, session_id="memory-probe")
    try:
        assert store.append("user_message", {"content": "one"}) == "memory-probe:1"
        assert store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": None}) == (
            "memory-probe:2"
        )
        assert [event["type"] for event in store.events_snapshot()] == [
            "user_message",
            TASK_STATE_EVENT,
        ]
        assert not store.path.exists()
    finally:
        store.close()
