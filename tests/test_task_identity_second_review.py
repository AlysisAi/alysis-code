"""Second independent review of the host-owned task identity (tree 62f00865):
the reviewer's fourteen probes integrated into the repository suite, plus the
boundary and matrix coverage behind the three corrected contracts.

H1 — exact accepted content, bounded presentation, reliable delivery.
H2 — complete schema and transition/state validation on recovery.
H3 — explicit no-log vs refused vs indeterminate persistence.

All model clients are deterministic and snapshot every request at call
time; nothing here talks to a provider.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.prompt_context import (
    _TASK_BRIEF_CONSTRAINTS_MAX_CHARS,
    _TASK_BRIEF_MAX_CHARS,
    _TASK_REQUIREMENTS_MARKER,
    _TASK_REQUIREMENTS_MAX_CHARS,
    _TASK_REQUIREMENTS_OVERHEAD_MAX_CHARS,
    _render_task_brief_from_state,
    _render_task_requirements_from_state,
    _session_task_brief_content,
    _session_task_requirements_content,
    _task_brief_objective_lines,
    task_brief_carries_full_objective,
    task_brief_carries_full_requirements,
    task_requirements_delivered_by_pinned_messages,
)
from alysis_code.agent.task_state import (
    TASK_STATE_EVENT,
    SessionTaskState,
    TaskPersistenceError,
    accept_session_task,
    recover_task_state_from_events,
    restore_session_task_state,
    transition_task_state,
    validate_task_state_payload,
)
from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.chat import loop as chat_loop_mod
from alysis_code.config import ConfigError
from alysis_code.llm.openai_compat import LLMError
from alysis_code.session_store import (
    SESSION_STORE_UNCOMMITTED_EVENT,
    SessionStore,
    SessionStoreUnavailableError,
    SessionStoreWriteError,
    read_session_events,
)
from tests.test_task_identity_hardening import (
    _dispatch_command,
    _events,
    _FailingClient,
    _final,
    _RecordingClient,
    _requirements,
    _resume_into,
    _session,
    _single_brief,
    _single_requirements,
    _user_texts,
)
from tests.test_task_identity_lifecycle import _compaction_cfg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(objective: str = "Review the parser.", **overrides: Any) -> SessionTaskState:
    fields: dict[str, Any] = dict(
        task_id="s:task:1", objective=objective, session_id="s", sequence=1, origin_event_id="s:1"
    )
    fields.update(overrides)
    return SessionTaskState(**fields)


def _state_payload() -> dict[str, Any]:
    return _state(amendments=("Do not modify public APIs.",)).to_payload()


def _reconstruct_brief_items(items: list[str]) -> str:
    """Read the exact request back out of ``- `` items (``-`` is a blank line)."""

    lines: list[str] = []
    for item in items:
        if item == "-":
            lines.append("")
        else:
            assert item.startswith("- "), item
            lines.append(item[2:])
    return "\n".join(lines)


def _brief_index(messages: list[dict[str, Any]]) -> int:
    return next(
        index
        for index, message in enumerate(messages)
        if str(message.get("content") or "").startswith("<task_brief>")
    )


def _disk(session: Any) -> Any:
    return recover_task_state_from_events(
        read_session_events(session.store.path), session_id=session.store.session_id
    )


def _memory(session: Any) -> Any:
    return recover_task_state_from_events(
        session.store.events_snapshot(), session_id=session.store.session_id
    )


class _FlushErrorAfterCompleteWrite:
    """Failure after real bytes reached the file, before ``append`` succeeds
    (the reviewer's fault model). Optionally limited to task-state records."""

    def __init__(self, inner: Any, *, only_task_state: bool = False) -> None:
        self._inner = inner
        self._only_task_state = only_task_state
        self._arm = False

    def write(self, text: str) -> int:
        self._arm = not self._only_task_state or f'"type": "{TASK_STATE_EVENT}"' in text
        return self._inner.write(text)

    def flush(self) -> None:
        self._inner.flush()
        if self._arm:
            raise OSError("injected flush completion error")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _BoundaryWritesBlocked:
    """Block the store's two ways of recording an uncommitted boundary."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, log_path: Path) -> None:
        self.log_path = log_path
        self.block_marker = True
        self.block_sidecar = True
        real_open = Path.open
        real_write_text = Path.write_text
        blocker = self

        def guarded_open(self_path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if blocker.block_marker and self_path == blocker.log_path and "a" in mode:
                raise OSError("injected marker append failure")
            return real_open(self_path, mode, *args, **kwargs)

        def guarded_write_text(self_path: Path, *args: Any, **kwargs: Any) -> Any:
            if blocker.block_sidecar and self_path.name.endswith(".uncommitted.json"):
                raise OSError("injected sidecar write failure")
            return real_write_text(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_open)
        monkeypatch.setattr(Path, "write_text", guarded_write_text)


def _fail_truncate(monkeypatch: pytest.MonkeyPatch) -> None:
    import alysis_code.session_store as stores

    def cannot_truncate(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected rollback truncation failure")

    monkeypatch.setattr(stores.os, "truncate", cannot_truncate)


# ===========================================================================
# H1 — exact accepted content, bounded presentation, reliable delivery
# ===========================================================================


_EXACT_OBJECTIVE = (
    "Review the parser.\n"
    "\n"
    '\tKeep the literal "a  b" (two spaces) and the tab above.\n'
    "    indented line with trailing spaces   \n"
    "MANDATORY: Do Not Change parse_record — Case Matters."
)


def test_h1_state_and_brief_are_exact_for_whitespace_indentation_and_case(tmp_path: Path) -> None:
    session = _session(tmp_path, mode="readonly")
    try:
        client = _RecordingClient([_final("Reviewed.")])
        session.client = client
        assert session.run_turn(_EXACT_OBJECTIVE) == 0
        assert session.task_state.objective == _EXACT_OBJECTIVE
        brief = _single_brief(client.requests[0])
        items = [
            line
            for line in brief.splitlines()
            if line == "-" or (line.startswith("- ") and not line.startswith("- ["))
        ]
        assert _reconstruct_brief_items(items) == _EXACT_OBJECTIVE
        assert '- \tKeep the literal "a  b" (two spaces) and the tab above.' in brief
        assert "-     indented line with trailing spaces   " in brief
        assert task_brief_carries_full_requirements(session.task_state)
        assert _requirements(client.requests[0]) == []
        # The persisted state and its replay are exact too.
        assert _disk(session).state.objective == _EXACT_OBJECTIVE
        payload = _events(session.store.path, TASK_STATE_EVENT)[-1]["state"]
        assert payload["objective"] == _EXACT_OBJECTIVE
    finally:
        session.close()


def test_h1_literal_whitespace_in_short_objective_survives_retry(tmp_path: Path) -> None:
    """Reviewer probe: a lossy projection must not be treated as complete."""

    session = _session(tmp_path, mode="readonly")
    objective = 'Review the parser. Preserve the exact literal "a  b" (two spaces).'
    try:
        session.client = _FailingClient()
        with pytest.raises(LLMError):
            session.run_turn(objective)
        assert session.task_state.objective == objective
        retry = _RecordingClient([_final("Continuing.")])
        session.client = retry
        assert session.run_turn("continue", task_relation="continuation") == 0
        contents = [
            message.get("content", "")
            for message in retry.requests[0]
            if isinstance(message.get("content"), str)
        ]
        assert any('"a  b"' in content for content in contents)
        assert _single_brief(retry.requests[0]).count('"a  b"') == 1
    finally:
        session.close()


def test_h1_case_sensitive_paths_are_distinct_accepted_constraints() -> None:
    """Reviewer probe: ``src/Parser.py`` and ``src/parser.py`` are two constraints."""

    initial = transition_task_state(
        None,
        instruction="Review the build.",
        relation="new_task",
        session_id="s",
        origin_event_id="s:1",
    ).state
    one = transition_task_state(
        initial,
        instruction="Keep src/Parser.py unchanged.",
        relation="amendment",
        session_id="s",
        origin_event_id="s:2",
    ).state
    two = transition_task_state(
        one,
        instruction="Keep src/parser.py unchanged.",
        relation="amendment",
        session_id="s",
        origin_event_id="s:3",
    ).state
    assert two.amendments == ("Keep src/parser.py unchanged.", "Keep src/Parser.py unchanged.")
    # Only an exact repeat folds; spacing differences are distinct constraints.
    repeat = transition_task_state(
        two,
        instruction="Keep src/parser.py unchanged.",
        relation="amendment",
        session_id="s",
        origin_event_id="s:4",
    )
    assert repeat.kind == "kept" and repeat.reason == "no_new_constraint"
    spaced = transition_task_state(
        two,
        instruction="Keep  src/parser.py unchanged.",
        relation="amendment",
        session_id="s",
        origin_event_id="s:5",
    ).state
    assert spaced.amendments[0] == "Keep  src/parser.py unchanged."
    assert len(spaced.amendments) == 3


def test_h1_amendment_is_stored_exactly_and_its_lines_are_a_derived_view() -> None:
    """Contract (third review): the amendment is one exact unit — blank lines,
    indentation, repeats and the objective's own text included. The line view
    (``AcceptedAmendments.lines`` / membership) is derived, never the stored
    content; earlier builds stored deduplicated non-blank lines instead."""

    initial = transition_task_state(
        None,
        instruction="Review the build.",
        relation="new_task",
        session_id="s",
        origin_event_id="s:1",
    ).state
    text = "Rules:\n\n    - keep   spacing\n\tTabbed Rule\n   \nReview the build.\r\nKEEP\nKEEP"
    amended = transition_task_state(
        initial, instruction=text, relation="amendment", session_id="s", origin_event_id="s:2"
    ).state
    assert amended.amendments == (text,)
    assert amended.amendments.lines() == (
        ("Rules:", "    - keep   spacing", "\tTabbed Rule", "Review the build.", "KEEP", "KEEP"),
    )
    assert "    - keep   spacing" in amended.amendments
    assert "KEEP" in amended.amendments
    assert "Rules:\n" not in amended.amendments
    assert "keep   spacing" not in amended.amendments
    # Exact through persistence and replay.
    state, why = validate_task_state_payload(amended.to_payload())
    assert why == "" and state == amended and state.amendments[0] == text


@pytest.mark.parametrize(
    "objective",
    [
        "x" * 10_000,
        "\n".join("a" for _ in range(6_000)),
        "\n".join(f"Requirement {index}: keep field {index} unchanged." for index in range(400)),
        "line\n" * 3_000,
        "\n" * 5 + "only blank lines then text" + "\n" * 5,
    ],
)
def test_h1_brief_projection_is_bounded_and_exact_for_any_line_structure(objective: str) -> None:
    state = _state(objective)
    items, omitted = _task_brief_objective_lines(objective)
    if omitted == 0:
        assert _reconstruct_brief_items(items) == objective
    else:
        # What is rendered is an exact prefix of the request and the announced
        # remainder is exactly the rest: a clipped single line resumes right
        # after its head, whole lines resume after their line break.
        suffix = objective[len(objective) - omitted :]
        if items[0].endswith("...") and len(items) == 1:
            assert items[0][2:-3] + suffix == objective
        else:
            assert _reconstruct_brief_items(items) + "\n" + suffix == objective
    brief = _render_task_brief_from_state(state, unrecovered=False)
    assert len(brief) <= _TASK_BRIEF_MAX_CHARS
    requirements = _render_task_requirements_from_state(state)
    if task_brief_carries_full_objective(state):
        assert requirements is None
    else:
        assert requirements is not None
        assert f"[accepted request continues: {omitted} more characters; " in brief
        assert f"<accepted_request>\n{objective}\n</accepted_request>" in requirements
        assert "delivery: complete" in requirements
        assert (
            len(requirements)
            <= _TASK_REQUIREMENTS_MAX_CHARS + _TASK_REQUIREMENTS_OVERHEAD_MAX_CHARS
        )


def test_h1_one_long_amendment_obeys_the_declared_pinned_brief_budget(tmp_path: Path) -> None:
    """Reviewer probe: a single constraint never bypasses the projection budget."""

    session = _session(tmp_path, mode="readonly")
    try:
        accept_session_task(session, instruction="Review the parser.")
        accept_session_task(session, instruction="Constraint: " + "a" * 25000, relation="amendment")
        brief = _session_task_brief_content(session)
        assert len(brief) < 7000
        assert len(brief) <= _TASK_BRIEF_MAX_CHARS
        assert "- Constraint: aaaa" in brief and "..." in brief
        assert "[1 accepted constraint(s) not shown in full; " in brief
        # 25,012 characters exceed the delivery budget too: the requirements
        # message says so instead of pretending to be complete.
        requirements = _session_task_requirements_content(session)
        assert "delivery: partial" in requirements
        assert "[1 accepted amendment(s) beyond the host delivery budget;" in requirements
        assert "Do not assume requirements you cannot see" in requirements
        assert not task_requirements_delivered_by_pinned_messages(session.task_state)
    finally:
        session.close()


def test_h1_many_constraints_are_bounded_in_the_brief_and_complete_in_requirements(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, mode="readonly")
    try:
        accept_session_task(session, instruction="Review the parser.")
        constraints = [
            f"Rule {index}: keep field {index} exactly as Documented." for index in range(150)
        ]
        for text in constraints:
            accept_session_task(session, instruction=text, relation="amendment")
        assert list(session.task_state.amendments) == list(reversed(constraints))
        brief = _session_task_brief_content(session)
        assert len(brief) <= _TASK_BRIEF_MAX_CHARS
        shown = [line for line in brief.splitlines() if line.startswith("- Rule ")]
        assert shown and shown[0] == "- Rule 149: keep field 149 exactly as Documented."
        assert sum(len(line) + 1 for line in shown) <= _TASK_BRIEF_CONSTRAINTS_MAX_CHARS
        assert f"[{150 - len(shown)} accepted constraint(s) not shown in full; " in brief
        requirements = _session_task_requirements_content(session)
        assert "delivery: complete" in requirements
        assert "accepted_amendments: 150, verbatim, newest first" in requirements
        assert all(
            f"<accepted_amendment>\n{text}\n</accepted_amendment>" in requirements
            for text in constraints
        )
        assert task_requirements_delivered_by_pinned_messages(session.task_state)
    finally:
        session.close()


def test_h1_long_accepted_amendment_survives_provider_error_and_continue(tmp_path: Path) -> None:
    """Reviewer probe: an amended transition is delivered after a rollback."""

    session = _session(tmp_path, mode="readonly")
    critical = "MANDATORY: Never change the compatibility export parse_legacy_record."
    amendment = "Background A: " + "a" * 966 + "\nBackground B: " + "b" * 966 + "\n" + critical
    try:
        session.client = _RecordingClient([_final("Started.")])
        assert session.run_turn("Review the parser.") == 0
        session.client = _FailingClient()
        with pytest.raises(LLMError):
            session.run_turn(amendment, task_relation="amendment")
        assert critical in session.task_state.amendments
        retry = _RecordingClient([_final("Continuing.")])
        session.client = retry
        assert session.run_turn("continue", task_relation="continuation") == 0
        first_request = retry.requests[0]
        requirements = _single_requirements(first_request)
        # The whole amendment, verbatim, as one block (third review): not a
        # line list.
        assert f"<accepted_amendment>\n{amendment}\n</accepted_amendment>" in requirements
        assert "delivery: complete" in requirements
        brief = _single_brief(first_request)
        assert "[1 accepted constraint(s) not shown in full; " in brief
        # Delivered by the pinned message, so the amendment's transcript copy
        # is not duplicated after the rollback.
        assert _user_texts(first_request)[-1] == "continue"
        assert amendment not in _user_texts(first_request)
    finally:
        session.close()


def test_h1_long_objective_critical_tail_survives_real_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe: compaction must not remove a mandatory requirement from
    the execution context. The requirements message is pinned, so the
    compactor never summarises it."""

    monkeypatch.setenv("ALYSIS_MODEL_COMPACTOR", "compactor-model")
    objective = (
        "Review the repository.\n"
        + "Background context. " * 260
        + "\nMANDATORY: Never change the public export function parse_record."
    )
    critical = "Never change the public export function parse_record."
    session = create_session(
        cfg=_compaction_cfg(),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / ".sessions",
        enable_compaction=True,
        verification_enabled=False,
    )
    try:
        summary = {
            "goal": "Review the repository",
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
        client = _RecordingClient([_final("Continuing the review.")])
        session.client = client
        assert session.run_turn("continue", task_relation="continuation") == 0
        events = list(read_session_events(session.store.path))
        assert any(event["type"] == "conversation_summary_updated" for event in events)
        assert session.task_state == original_state
        assert all(critical in json.dumps(request) for request in client.requests)
        # It is the pinned requirements message that carries it.
        requirements = _single_requirements(client.requests[0])
        assert f"<accepted_request>\n{objective}\n</accepted_request>" in requirements
        assert objective not in _user_texts(client.requests[0])
        # And the pinned prefix still covers it after compaction.
        assert _brief_index(session.messages) + 1 < session.pinned_prefix_len
        assert session.conversation_compactor.state.pinned_prefix_len == session.pinned_prefix_len
    finally:
        session.close()


def test_h1_requirements_message_is_pinned_removed_on_clear_and_rerendered_on_rollover(
    tmp_path: Path,
) -> None:
    long_request = "\n".join(f"Requirement {index}: keep field {index}." for index in range(400))
    session = _session(tmp_path, mode="readonly")
    try:
        client = _RecordingClient([_final("ok")])
        session.client = client
        before_len = session.pinned_prefix_len
        assert session.run_turn(long_request) == 0
        assert session.pinned_prefix_len == before_len + 1
        index = _brief_index(session.messages)
        assert str(session.messages[index + 1]["content"]).startswith(_TASK_REQUIREMENTS_MARKER)
        assert index + 1 < session.pinned_prefix_len

        # A TUI history rollover keeps the objective and re-renders both
        # pinned messages from state; the prefix length stays consistent.
        # (A command dispatch through the CLI facade binds the chat loop's
        # runtime globals exactly as production does, without monkeypatching
        # module state.)
        _dispatch_command("/objective", session=session, tmp_path=tmp_path)
        chat_loop_mod._reset_chat_conversation(
            session=session,
            trigger="captured_duplicate_subagent_result",
            retained_system_messages=(
                "<subagent_lifecycle_capsule>kept</subagent_lifecycle_capsule>",
            ),
        )
        assert session.task_state.objective == long_request
        assert len(_requirements(session.messages)) == 1
        assert session.pinned_prefix_len == before_len + 1
        index = _brief_index(session.messages)
        assert str(session.messages[index + 1]["content"]).startswith(_TASK_REQUIREMENTS_MARKER)
        assert session.messages[-1]["role"] == "system"
        follow = _RecordingClient([_final("ok")])
        session.client = follow
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert f"<accepted_request>\n{long_request}\n</accepted_request>" in _single_requirements(
            follow.requests[0]
        )

        # /clear retires the task: the requirements message goes with it.
        _dispatch_command("/clear", session=session, tmp_path=tmp_path)
        assert session.task_state is None
        assert _requirements(session.messages) == []
        assert session.pinned_prefix_len == before_len
        short = _RecordingClient([_final("ok")])
        session.client = short
        assert session.run_turn("Short task.") == 0
        assert _requirements(short.requests[0]) == []
        assert session.pinned_prefix_len == before_len
    finally:
        session.close()


def test_h1_requirements_are_rendered_from_state_alone_after_cli_resume(tmp_path: Path) -> None:
    long_request = "\n".join(f"Requirement {index}: keep field {index}." for index in range(400))
    original = _session(tmp_path, mode="readonly", session_id_override="second-review-long-source")
    try:
        original.client = _RecordingClient([_final("ok")])
        assert original.run_turn(long_request) == 0
    finally:
        original.close()
    resumed = _session(tmp_path, mode="readonly", session_id_override="second-review-long-target")
    try:
        ok, message = _resume_into(resumed, "second-review-long-source")
        assert ok, message
        assert resumed.task_state.objective == long_request
        assert f"<accepted_request>\n{long_request}\n</accepted_request>" in (
            _session_task_requirements_content(resumed)
        )
        client = _RecordingClient([_final("ok")])
        resumed.client = client
        assert resumed.run_turn("continue", task_relation="continuation") == 0
        assert f"<accepted_request>\n{long_request}\n</accepted_request>" in _single_requirements(
            client.requests[0]
        )
        assert _brief_index(resumed.messages) + 1 < resumed.pinned_prefix_len
    finally:
        resumed.close()


# ===========================================================================
# H2 — complete schema and transition/state validation
# ===========================================================================


@pytest.mark.parametrize(
    "damage",
    [
        {"objective": {"unexpected": "object"}},
        {"amendments": "Do not modify public APIs."},
        {"amendments": [None]},
        {"task_id": "other:task:99"},
    ],
)
def test_h2_invalid_latest_state_fields_are_not_coerced_into_authority(
    damage: dict[str, Any],
) -> None:
    """Reviewer probe."""

    payload = _state_payload()
    payload.update(damage)
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
    assert recovered.recovery == "unrecoverable" and recovered.state is None


@pytest.mark.parametrize(
    "transition", ["cleared", "restore_unrecoverable", "not-a-known-transition"]
)
def test_h2_invalid_transition_with_active_state_is_not_treated_as_accepted(
    transition: str,
) -> None:
    """Reviewer probe."""

    recovered = recover_task_state_from_events(
        [
            {
                "type": TASK_STATE_EVENT,
                "session_id": "s",
                "payload": {"transition": transition, "state": _state_payload()},
            }
        ],
        session_id="s",
    )
    assert recovered.recovery == "unrecoverable" and recovered.state is None


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ({"sequence": "1"}, "state_sequence_invalid"),
        ({"sequence": 0}, "state_sequence_invalid"),
        ({"sequence": True}, "state_sequence_invalid"),
        ({"sequence": 2}, "state_task_id_inconsistent"),
        ({"session_id": "other"}, "state_task_id_inconsistent"),
        ({"task_id": ""}, "state_task_id_invalid"),
        ({"objective": ""}, "state_objective_invalid"),
        ({"objective": "   \n"}, "state_objective_invalid"),
        ({"objective": 42}, "state_objective_invalid"),
        ({"origin": "model_guess"}, "state_origin_invalid"),
        ({"origin": None}, "state_origin_invalid"),
        ({"accepted_at": 12}, "state_accepted_at_invalid"),
        ({"origin_event_id": 7}, "state_origin_event_id_invalid"),
        ({"request_id": ""}, "state_request_id_invalid"),
        ({"parent_session_id": ["p"]}, "state_parent_session_id_invalid"),
        ({"resumed_from_session_id": 1}, "state_resumed_from_session_id_invalid"),
        ({"amendments": ["ok", ""]}, "state_amendments_invalid"),
        # Present ``null`` is not an empty list (third review).
        ({"amendments": None}, "state_amendments_invalid"),
        ({"prior_objectives": None}, "state_prior_objectives_invalid"),
        ({"amendments": {"a": 1}}, "state_amendments_invalid"),
        ({"prior_objectives": ["a", "b", "c", "d"]}, "state_prior_objectives_invalid"),
        ({"prior_objectives": [1]}, "state_prior_objectives_invalid"),
        ({"objective_truncated": "yes"}, "state_objective_truncated_invalid"),
        ({"schema_version": 2}, "unsupported_task_state_schema"),
        ({"schema_version": "1"}, "unsupported_task_state_schema"),
        ({"extra": True}, "state_unknown_fields:extra"),
    ],
)
def test_h2_state_schema_matrix_rejects_every_invalid_field(
    damage: dict[str, Any], reason: str
) -> None:
    payload = _state_payload()
    payload.update(damage)
    state, why = validate_task_state_payload(payload)
    assert state is None and why == reason
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
    assert recovered.recovery == "unrecoverable" and recovered.state is None
    assert reason.split(":")[0] in recovered.reason


@pytest.mark.parametrize("missing", ["task_id", "objective", "session_id", "sequence"])
def test_h2_missing_required_field_is_unrecoverable(missing: str) -> None:
    payload = _state_payload()
    del payload[missing]
    state, why = validate_task_state_payload(payload)
    assert state is None and why == f"state_missing_fields:{missing}"


def test_h2_valid_states_round_trip_and_legacy_shapes_stay_supported() -> None:
    full = _state(
        amendments=("Keep it.", "    indented constraint"),
        prior_objectives=("older", "oldest"),
        request_id="prompt-1",
        parent_session_id="parent",
        accepted_at="2026-09-16T00:00:00+00:00",
    )
    state, why = validate_task_state_payload(full.to_payload())
    assert why == "" and state == full
    # First-format payload: only the required keys plus what early writers set.
    first_format = {
        "task_id": "s:task:1",
        "objective": "Review the parser.",
        "session_id": "s",
        "sequence": 1,
        "origin": "user_instruction",
        "origin_event_id": "s:1",
        "accepted_at": "",
        "amendments": [],
        "prior_objectives": [],
        "resumed_from_session_id": None,
    }
    state, why = validate_task_state_payload(first_format)
    assert why == "" and state is not None
    assert state.request_id is None and state.parent_session_id is None
    assert state.objective_truncated is False
    # Optional keys entirely absent are the defaults, not an error.
    minimal = {
        "task_id": "s:task:1",
        "objective": "Review the parser.",
        "session_id": "s",
        "sequence": 1,
    }
    state, why = validate_task_state_payload(minimal)
    assert why == "" and state is not None and state.origin == "user_instruction"
    assert state.amendments == () and state.prior_objectives == ()
    # An explicitly empty list is valid; a multi-line member is one exact
    # amendment (third review), and a record from an earlier build that
    # stored one line per member replays each member as its own unit.
    state, why = validate_task_state_payload({**minimal, "amendments": [], "prior_objectives": []})
    assert why == "" and state is not None and state.amendments == ()
    state, why = validate_task_state_payload(
        {**minimal, "amendments": ["BEGIN\nKEEP\nKEEP\nEND", "one"]}
    )
    assert why == "" and state is not None
    assert state.amendments == ("BEGIN\nKEEP\nKEEP\nEND", "one")
    assert "KEEP" in state.amendments and "one" in state.amendments
    # A legitimately resumed state keeps its original owner's ids: consistent
    # within itself, and recovery does not confuse the destination session
    # with the original owner.
    carried = SessionTaskState(
        task_id="source:task:3",
        objective="Carried over.",
        session_id="source",
        sequence=3,
        origin="resumed",
        resumed_from_session_id="source",
    )
    recovered = recover_task_state_from_events(
        [
            {"type": "session_start", "session_id": "target", "payload": {"task_state_schema": 1}},
            {
                "type": TASK_STATE_EVENT,
                "session_id": "target",
                "payload": {
                    "transition": "restored",
                    "relation": "host",
                    "state": carried.to_payload(),
                },
            },
        ],
        session_id="target",
    )
    assert recovered.recovery == "event" and recovered.state == carried
    assert recovered.event_session_id == "target"


@pytest.mark.parametrize(
    ("payload", "reason_prefix"),
    [
        ({"transition": "accepted", "state": None}, "latest_task_state_missing_active_state"),
        ({"transition": "restored", "state": None}, "latest_task_state_missing_active_state"),
        (
            {"transition": "rolled_back", "state": "not-null"},
            "latest_task_state_transition_incompatible",
        ),
        (
            {"transition": "restore_refused", "state": {}},
            "latest_task_state_transition_incompatible",
        ),
        ({"transition": 7, "state": None}, "latest_task_state_transition_unknown"),
        ({"state": None}, "latest_task_state_transition_unknown"),
        (
            {"transition": "accepted", "relation": "guess", "state": None},
            "latest_task_state_relation_invalid",
        ),
        (
            {"transition": "cleared", "relation": 3, "state": None},
            "latest_task_state_relation_invalid",
        ),
    ],
)
def test_h2_transition_state_matrix_rejects_contradictions(
    payload: dict[str, Any], reason_prefix: str
) -> None:
    recovered = recover_task_state_from_events(
        [{"type": TASK_STATE_EVENT, "session_id": "s", "payload": payload}], session_id="s"
    )
    assert recovered.recovery == "unrecoverable" and recovered.state is None
    assert recovered.reason.startswith(reason_prefix)


def test_h2_valid_null_state_transitions_keep_their_meaning() -> None:
    for transition in ("cleared", "restore_none", "rolled_back"):
        recovered = recover_task_state_from_events(
            [
                {
                    "type": TASK_STATE_EVENT,
                    "session_id": "s",
                    "payload": {"transition": transition, "relation": "host", "state": None},
                }
            ],
            session_id="s",
        )
        assert recovered.recovery == "none" and recovered.state is None
    for transition in ("restore_unrecoverable", "restore_refused"):
        recovered = recover_task_state_from_events(
            [
                {
                    "type": TASK_STATE_EVENT,
                    "session_id": "s",
                    "payload": {
                        "transition": transition,
                        "relation": "host",
                        "state": None,
                        "reason": "why",
                    },
                }
            ],
            session_id="s",
        )
        assert recovered.recovery == "unrecoverable"
        assert recovered.reason == f"{transition}_persisted:why"


def test_h2_invalid_latest_authority_stays_unrecoverable_through_restore_and_continue(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, mode="readonly")
    try:
        recovered = recover_task_state_from_events(
            [
                {
                    "type": TASK_STATE_EVENT,
                    "session_id": session.store.session_id,
                    "payload": {"transition": "cleared", "state": _state_payload()},
                }
            ],
            session_id=session.store.session_id,
        )
        assert recovered.recovery == "unrecoverable"
        restore_session_task_state(session, recovered, source_session_id=session.store.session_id)
        assert session.task_state is None and session.task_state_unrecovered
        assert _disk(session).recovery == "unrecoverable"
        client = _RecordingClient([_final("Need the original task.")])
        session.client = client
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert session.task_state is None and session.task_state_unrecovered
        # Only an explicit new task re-establishes identity.
        session.client = _RecordingClient([_final("ok")])
        assert session.run_turn("Fresh task.", task_relation="new_task") == 0
        assert session.task_state is not None and not session.task_state_unrecovered
        assert _disk(session).state == session.task_state
    finally:
        session.close()


# ===========================================================================
# H3 — explicit no-log vs refused vs indeterminate persistence
# ===========================================================================


def test_h3_log_open_failure_is_not_silently_treated_as_explicit_no_log(tmp_path: Path) -> None:
    """Reviewer probe: durable mode requested, the sessions directory is an
    ordinary file. The session refuses to start (fail closed) with a message
    that names the explicit alternative."""

    blocked = tmp_path / "blocked-sessions"
    blocked.write_text("not a directory")
    with pytest.raises(ConfigError) as excinfo:
        _session(tmp_path, mode="readonly", session_log_dir_override=blocked)
    message = str(excinfo.value)
    assert "Session log unavailable" in message
    assert "--no-log" in message
    assert isinstance(excinfo.value.__cause__, SessionStoreUnavailableError)
    assert excinfo.value.__cause__.outcome == "unavailable"


def test_h3_deliberate_no_log_still_runs_without_a_file(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked-sessions"
    blocked.write_text("not a directory")
    session = _session(tmp_path, mode="readonly", no_log=True, session_log_dir_override=blocked)
    try:
        client = _RecordingClient([_final("Started.")])
        session.client = client
        assert session.run_turn("Review the parser.") == 0
        assert session.store.enabled is False
        assert len(client.requests) == 1
        assert session.task_state is not None
        assert _memory(session).state == session.task_state
    finally:
        session.close()
    assert blocked.read_text() == "not a directory"


def test_h3_refused_record_is_not_recovered_when_truncation_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe: a fully written but unpublished record must not become
    authoritative on disk. The store records an uncommitted-record boundary
    that replay honours."""

    store = SessionStore(
        enabled=True, sessions_dir=tmp_path, session_id="s", cwd=str(tmp_path), repo_root=None
    )
    try:
        original = _state_payload()
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": original})
        store._fh = _FlushErrorAfterCompleteWrite(store._fh)  # type: ignore[assignment]
        _fail_truncate(monkeypatch)
        replacement = copy.deepcopy(original)
        replacement.update(objective="Task B.", sequence=2, task_id="s:task:2")
        with pytest.raises(SessionStoreWriteError) as excinfo:
            store.append(TASK_STATE_EVENT, {"transition": "replaced", "state": replacement})
        assert excinfo.value.outcome == "refused"
        assert store.integrity_uncertain is False
        disk = recover_task_state_from_events(read_session_events(store.path), session_id="s")
        memory = recover_task_state_from_events(store.events_snapshot(), session_id="s")
        assert memory.state.objective == original["objective"]
        assert disk.state == memory.state
        # The bytes are still there; the boundary marker is what voids them.
        raw = store.path.read_text(encoding="utf-8")
        assert '"objective": "Task B."' in raw
        assert f'"type": "{SESSION_STORE_UNCOMMITTED_EVENT}"' in raw
        assert not any(
            event["type"] == SESSION_STORE_UNCOMMITTED_EVENT
            for event in read_session_events(store.path)
        )
        # The store keeps working afterwards and later records are read back.
        store._fh = None  # type: ignore[assignment]
        store.append("note", {"n": 1})
        replay = list(read_session_events(store.path))
        assert [event["type"] for event in replay] == [TASK_STATE_EVENT, "note"]
        assert replay == store.events_snapshot()
        # A fresh store hydrating the same log sees exactly the replay.
        store.close()
        reopened = SessionStore(
            enabled=True, sessions_dir=tmp_path, session_id="s", cwd=str(tmp_path), repo_root=None
        )
        try:
            assert [event["type"] for event in reopened.events_snapshot()] == [
                TASK_STATE_EVENT,
                "note",
            ]
            assert reopened.integrity_uncertain is False
        finally:
            reopened.close()
    finally:
        store.close()


def test_h3_sidecar_boundary_is_honoured_and_folded_into_the_log_on_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(
        enabled=True, sessions_dir=tmp_path, session_id="s", cwd=str(tmp_path), repo_root=None
    )
    try:
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": _state_payload()})
        store._fh = _FlushErrorAfterCompleteWrite(store._fh)  # type: ignore[assignment]
        _fail_truncate(monkeypatch)
        blocker = _BoundaryWritesBlocked(monkeypatch, store.path)
        blocker.block_sidecar = False  # the marker cannot be appended; the sidecar can
        with pytest.raises(SessionStoreWriteError) as excinfo:
            store.append("note", {"phantom": True})
        assert excinfo.value.outcome == "refused"
        # Contract (third review): the refusal is durable through the sidecar,
        # but the in-log marker is still owed, and no record is appended
        # before it — otherwise a later append would leave the disowned bytes
        # behind an ordinary record. So the store reports the marker as owed
        # and refuses records while the log cannot take it.
        assert store.integrity_uncertain is True
        sidecar = store.path.with_name(store.path.name + ".uncommitted.json")
        assert sidecar.exists()
        assert '"phantom": true' in store.path.read_text(encoding="utf-8")
        assert [event["type"] for event in read_session_events(store.path)] == [TASK_STATE_EVENT]
        assert list(read_session_events(store.path)) == store.events_snapshot()
        with pytest.raises(SessionStoreWriteError) as refused:
            store.append("note", {"after_sidecar": True})
        assert refused.value.outcome == "indeterminate"
        assert [event["type"] for event in read_session_events(store.path)] == [TASK_STATE_EVENT]
    finally:
        store.close()
    monkeypatch.undo()
    reopened = SessionStore(
        enabled=True, sessions_dir=tmp_path, session_id="s", cwd=str(tmp_path), repo_root=None
    )
    try:
        assert not sidecar.exists()
        raw = reopened.path.read_text(encoding="utf-8")
        assert f'"type": "{SESSION_STORE_UNCOMMITTED_EVENT}"' in raw
        assert [event["type"] for event in reopened.events_snapshot()] == [TASK_STATE_EVENT]
        reopened.append("note", {"after": True})
        assert [event["type"] for event in read_session_events(reopened.path)] == [
            TASK_STATE_EVENT,
            "note",
        ]
        assert list(read_session_events(reopened.path)) == reopened.events_snapshot()
    finally:
        reopened.close()


def test_h3_indeterminate_outcome_refuses_records_until_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(
        enabled=True, sessions_dir=tmp_path, session_id="s", cwd=str(tmp_path), repo_root=None
    )
    try:
        store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": _state_payload()})
        store._fh = _FlushErrorAfterCompleteWrite(store._fh)  # type: ignore[assignment]
        _fail_truncate(monkeypatch)
        blocker = _BoundaryWritesBlocked(monkeypatch, store.path)
        with pytest.raises(SessionStoreWriteError) as excinfo:
            store.append("note", {"phantom": True})
        assert excinfo.value.outcome == "indeterminate"
        assert "indeterminate" in str(excinfo.value)
        assert store.integrity_uncertain is True
        size_after_failure = store.path.stat().st_size
        # Every further record is refused, and nothing more reaches the log.
        with pytest.raises(SessionStoreWriteError) as refused:
            store.append("note", {"next": True})
        assert refused.value.outcome == "indeterminate"
        assert store.path.stat().st_size == size_after_failure
        assert [event["type"] for event in store.events_snapshot()] == [TASK_STATE_EVENT]
        assert store.reconcile() is False
        # Once the boundary can be recorded, the log is reconciled and the
        # phantom record is voided for every reader.
        blocker.block_marker = False
        assert store.reconcile() is True
        assert store.integrity_uncertain is False
        assert [event["type"] for event in read_session_events(store.path)] == [TASK_STATE_EVENT]
        store.append("note", {"after": True})
        assert [event["type"] for event in read_session_events(store.path)] == [
            TASK_STATE_EVENT,
            "note",
        ]
        assert list(read_session_events(store.path)) == store.events_snapshot()
    finally:
        store.close()


def test_h3_close_records_a_pending_boundary_when_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(
        enabled=True, sessions_dir=tmp_path, session_id="s", cwd=str(tmp_path), repo_root=None
    )
    store.append(TASK_STATE_EVENT, {"transition": "accepted", "state": _state_payload()})
    store._fh = _FlushErrorAfterCompleteWrite(store._fh)  # type: ignore[assignment]
    _fail_truncate(monkeypatch)
    blocker = _BoundaryWritesBlocked(monkeypatch, store.path)
    with pytest.raises(SessionStoreWriteError):
        store.append("note", {"phantom": True})
    assert store.integrity_uncertain
    blocker.block_marker = False
    store.close()
    assert [event["type"] for event in read_session_events(store.path)] == [TASK_STATE_EVENT]


def test_h3_indeterminate_task_acceptance_refuses_the_session_until_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the turn runtime: the acceptance fails after bytes
    reached the log, nothing can record the boundary, the previous task stays,
    no model request is made, and the next turn is refused too until the log
    is reconciled — after which disk and memory agree again."""

    session = _session(tmp_path, mode="readonly")
    try:
        session.client = _RecordingClient([_final("Acknowledged.")])
        assert session.run_turn("Task A: review the CLI.") == 0
        task_a = session.task_state
        session.store._fh = _FlushErrorAfterCompleteWrite(session.store._fh, only_task_state=True)  # type: ignore[assignment]
        _fail_truncate(monkeypatch)
        blocker = _BoundaryWritesBlocked(monkeypatch, session.store.path)
        client = _RecordingClient([_final("Should not be called.")])
        session.client = client
        assert session.run_turn("Task B: review the server.", task_relation="new_task") == 1
        assert not client.requests
        assert session.task_state == task_a
        assert session.store.integrity_uncertain is True
        assert _memory(session).state == task_a
        # Normal execution is refused while the outcome is indeterminate.
        with pytest.raises(SessionStoreWriteError) as refused:
            session.run_turn("continue", task_relation="continuation")
        assert refused.value.outcome == "indeterminate"
        assert not client.requests
        assert session.task_state == task_a
        # Reconciliation: the boundary is recorded, the phantom is voided.
        blocker.block_marker = False
        assert session.store.reconcile() is True
        assert _disk(session).state == task_a
        follow = _RecordingClient([_final("Continuing A.")])
        session.client = follow
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert len(follow.requests) == 1
        assert session.task_state == task_a
        assert _disk(session).state == _memory(session).state == task_a
        assert _single_brief(follow.requests[0]).count("- Task A: review the CLI.") == 1
    finally:
        session.close()


def test_h3_refused_acceptance_with_boundary_marker_keeps_disk_and_memory_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, mode="readonly")
    try:
        session.client = _RecordingClient([_final("Acknowledged.")])
        assert session.run_turn("Task A: review the CLI.") == 0
        task_a = session.task_state
        session.store._fh = _FlushErrorAfterCompleteWrite(session.store._fh, only_task_state=True)  # type: ignore[assignment]
        _fail_truncate(monkeypatch)
        client = _RecordingClient([_final("Should not be called.")])
        session.client = client
        with pytest.raises(TaskPersistenceError):
            accept_session_task(
                session, instruction="Task B: review the server.", relation="new_task"
            )
        assert session.task_state == task_a
        assert session.store.integrity_uncertain is False
        assert _disk(session).state == _memory(session).state == task_a
        assert '"objective": "Task B: review the server."' in session.store.path.read_text(
            encoding="utf-8"
        )
        session.store._fh = None  # type: ignore[assignment]
        follow = _RecordingClient([_final("Continuing A.")])
        session.client = follow
        assert session.run_turn("continue", task_relation="continuation") == 0
        assert _disk(session).state == _memory(session).state == task_a
        assert os.path.exists(session.store.path)
    finally:
        session.close()
