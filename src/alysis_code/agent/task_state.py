"""Host-owned task identity for an agent session.

The session host — not the model, not the rendered prompt, and not the count
of files a turn happened to edit — owns which task is being worked on.
``SessionTaskState`` is that record. The pinned ``<task_brief>`` message is a
bounded rendering of it (see ``prompt_context.refresh_session_task_brief_message``)
and never a second, independently inferred source of truth.

Trust boundary: the only inputs that can create or replace a task are the
instruction a host caller accepted for a turn (persisted as a ``user_message``
event) and a deliberate host transition (``/clear``, a caller-declared
``task_relation``). Tool output, repository content, retrieved documents,
compaction summaries, child-agent results, and prompt text that merely looks
like a host marker never reach these functions. Storing user text here keeps
its user trust level; it is not promoted to a system-level instruction.

Task state is identity only. It grants no permission to write, execute, bypass
approvals, or keep working, and it is not evidence that anything succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal

# Persisted on every transition. The latest event after the last conversation
# boundary is authoritative on resume.
TASK_STATE_EVENT = "session_task_state"

# A ``conversation_cleared`` event retires the task only when the user asked
# for it. Other triggers (the typed TUI history rollover) reset model-visible
# history but keep the objective, so they are not task boundaries.
TASK_BOUNDARY_EVENTS = frozenset({"conversation_cleared"})
TASK_BOUNDARY_TRIGGERS = frozenset({"user_command"})

TaskRelation = Literal["auto", "new_task", "continuation", "amendment"]
TASK_RELATIONS: tuple[str, ...] = ("auto", "new_task", "continuation", "amendment")

TaskOrigin = Literal["user_instruction", "delegated", "resumed", "recovered_legacy_user_events"]
TASK_ORIGINS: frozenset[str] = frozenset(
    {"user_instruction", "delegated", "resumed", "recovered_legacy_user_events"}
)

# Where an instruction offered to the task machine came from. ``accepted_turn``
# is input a host caller accepted for a turn (the chat loop after command
# parsing, a host prompt queue, a one-shot run, a delegated child task): the
# host already separated it from control commands, so its text is a task
# candidate whatever it starts with. ``legacy_log`` is a ``user_message``
# event replayed from a log written before task state existed, where the
# only evidence is the text itself and control-shaped single lines are kept
# out of the objective.
TaskInputProvenance = Literal["accepted_turn", "legacy_log"]

# The accepted objective is stored complete and exact — the accepted text
# itself, with its whitespace, indentation, line structure and case. This is
# a hard ceiling against a pathological paste, not a prompt budget (the
# pinned brief and the requirements delivery have their own); reaching it is
# recorded explicitly on the state, never silently.
TASK_OBJECTIVE_MAX_CHARS = 200_000
# Identity history (objectives a task replaced) stays bounded; it is not a
# requirement of the current task.
TASK_PRIOR_OBJECTIVES_MAX = 3

TASK_STATE_SCHEMA_VERSION = 1
_STATE_SCHEMA_VERSION = TASK_STATE_SCHEMA_VERSION

# Every key a schema-1 state payload may carry. ``to_payload`` writes all of
# them; first-format payloads (before ``request_id`` / ``parent_session_id`` /
# ``objective_truncated`` existed) omit the optional ones, which default.
_STATE_REQUIRED_KEYS = frozenset({"task_id", "objective", "session_id", "sequence"})
_STATE_OPTIONAL_KEYS = frozenset(
    {
        "schema_version",
        "origin",
        "origin_event_id",
        "accepted_at",
        "amendments",
        "prior_objectives",
        "resumed_from_session_id",
        "request_id",
        "parent_session_id",
        "objective_truncated",
    }
)
_STATE_KNOWN_KEYS = _STATE_REQUIRED_KEYS | _STATE_OPTIONAL_KEYS

# Written by the session store itself (never through ``append``) after a
# failed write it could neither truncate away nor locate. ``read_session_events``
# applies every located boundary silently; a boundary that cannot say where
# the failed attempt began is yielded with ``payload.unidentified`` so task
# recovery can refuse explicitly rather than guess which record it disowns.
UNCOMMITTED_BOUNDARY_EVENT = "session_store_uncommitted"

# Transitions a persisted ``session_task_state`` event may carry, by what they
# assert. A transition that carries an active task (``state`` is an object)
# must be one of the first set; ``state: null`` is only valid with a retiring
# or an unrecovered transition. Any other pairing is a contradiction and is
# reported as unrecoverable, never read as an active or as a cleared task.
_ACTIVE_STATE_TRANSITIONS = frozenset({"accepted", "replaced", "amended", "restored"})
_NO_TASK_TRANSITIONS = frozenset({"cleared", "restore_none", "rolled_back"})
_UNRECOVERED_TRANSITIONS = frozenset({"restore_unrecoverable", "restore_refused"})
_KNOWN_TRANSITIONS = _ACTIVE_STATE_TRANSITIONS | _NO_TASK_TRANSITIONS | _UNRECOVERED_TRANSITIONS
# ``relation`` on a persisted transition: the caller's task relation for an
# instruction transition, ``host`` for a deliberate host transition.
_TRANSITION_RELATIONS = frozenset({*TASK_RELATIONS, "host"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AcceptedAmendments(tuple[str, ...]):
    """The exact accepted amendments of a task, newest first.

    Each member is one accepted amendment *as written* — its line order,
    repeated lines, blank lines, indentation and line endings included. It is
    a plain tuple of strings for equality, iteration, indexing and
    serialisation. Membership answers "is this text an accepted requirement":
    ``text in amendments`` is true for a whole amendment and also for any
    single line of one, so that a constraint stated on one line of a longer
    amendment can still be asked about directly. The derived line view
    (:meth:`lines`) is a bounded summary for prompt projection; it is never
    the authoritative content.
    """

    __slots__ = ()

    def __contains__(self, item: object) -> bool:
        if tuple.__contains__(self, item):
            return True
        if not isinstance(item, str) or "\n" in item or "\r" in item:
            return False
        return any(item in _amendment_lines(text) for text in self)

    def lines(self) -> tuple[tuple[str, ...], ...]:
        """Every amendment's non-blank lines, exactly, in order (newest amendment first)."""

        return tuple(tuple(_amendment_lines(text)) for text in self)

    def __repr__(self) -> str:
        return tuple.__repr__(self)


@dataclass(frozen=True)
class SessionTaskState:
    """The authoritative record of the task a session is working on."""

    task_id: str
    objective: str
    session_id: str
    sequence: int
    origin: str = "user_instruction"
    origin_event_id: str | None = None
    accepted_at: str = ""
    # Explicit follow-up amendments accepted for this task, newest first, each
    # exactly as accepted (see :class:`AcceptedAmendments`).
    amendments: tuple[str, ...] = ()
    # Objectives this task replaced, newest first (identity history only).
    prior_objectives: tuple[str, ...] = ()
    # Set when the state was carried over from a different session log.
    resumed_from_session_id: str | None = None
    # Host-supplied identity of the accepted request (for example a queued
    # prompt id). A re-dispatch of the same request keeps the same task; a
    # different request with identical text is a different task.
    request_id: str | None = None
    # Session id of the parent that delegated this task (child sessions only).
    parent_session_id: str | None = None
    # True only when the accepted request exceeded TASK_OBJECTIVE_MAX_CHARS and
    # ``objective`` holds its head; the complete text is in the originating
    # ``user_message`` event. Never set for ordinary requests.
    objective_truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.amendments, AcceptedAmendments):
            object.__setattr__(self, "amendments", AcceptedAmendments(self.amendments))
        if not isinstance(self.prior_objectives, tuple):
            object.__setattr__(self, "prior_objectives", tuple(self.prior_objectives))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "task_id": self.task_id,
            "objective": self.objective,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "origin": self.origin,
            "origin_event_id": self.origin_event_id,
            "accepted_at": self.accepted_at,
            "amendments": list(self.amendments),
            "prior_objectives": list(self.prior_objectives),
            "resumed_from_session_id": self.resumed_from_session_id,
            "request_id": self.request_id,
            "parent_session_id": self.parent_session_id,
            "objective_truncated": self.objective_truncated,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> SessionTaskState | None:
        """Rebuild a state from a persisted payload; ``None`` when it is not valid.

        Validation is complete and strict: every field must have the type the
        schema gives it (nothing is stringified, coerced or silently dropped),
        the task id must be the one its own ``session_id``/``sequence`` produce,
        and only the keys of the supported schema may be present. A payload
        written by a newer (or unknown) schema is not guessed at:
        ``schema_version`` must be absent (first-format events) or equal to
        :data:`TASK_STATE_SCHEMA_VERSION`. :func:`validate_task_state_payload`
        reports why a payload is rejected.

        Compatibility: ``amendments`` members are the exact accepted
        amendments. Records written by earlier builds of this branch stored
        one *line* of an amendment per member (blank lines and repeats
        dropped); such members are replayed as they are — each as its own
        exact unit — never re-joined or otherwise reconstructed into the
        amendment they came from, which the record no longer holds. An
        omitted list field is the documented first-format default (empty); a
        present ``null`` is invalid.
        """

        state, _reason = validate_task_state_payload(payload)
        return state


def _optional_str(payload: dict[str, Any], key: str) -> tuple[str | None, bool]:
    """``(value, valid)`` for a field that is ``None``/absent or a non-empty string."""

    raw = payload.get(key)
    if raw is None:
        return None, True
    if isinstance(raw, str) and raw.strip():
        return raw, True
    return None, False


_MISSING = object()


def _string_list(value: Any, *, limit: int | None = None) -> tuple[str, ...] | None:
    """A persisted list of accepted text, exactly as stored; ``None`` when invalid.

    The field must be present as a list (``[]`` is an explicitly empty one);
    an absent field is the caller's business (``_MISSING`` → default), but a
    present ``null`` or any other type is invalid. Every member must be a
    non-blank string, kept exactly (an accepted amendment may span several
    lines). An invalid member invalidates the whole value: it is never
    dropped or stringified.
    """

    if value is _MISSING:
        return ()
    if not isinstance(value, list | tuple):
        return None
    if limit is not None and len(value) > limit:
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        out.append(item)
    return tuple(out)


def validate_task_state_payload(payload: Any) -> tuple[SessionTaskState | None, str]:
    """Strictly validate a persisted state payload; ``(state, "")`` or ``(None, reason)``."""

    if not isinstance(payload, dict):
        return None, "state_not_an_object"
    unknown = set(payload) - _STATE_KNOWN_KEYS
    if unknown:
        return None, "state_unknown_fields:" + ",".join(sorted(str(key) for key in unknown))
    missing = _STATE_REQUIRED_KEYS - set(payload)
    if missing:
        return None, "state_missing_fields:" + ",".join(sorted(missing))
    schema_raw = payload.get("schema_version", _STATE_SCHEMA_VERSION)
    if (
        isinstance(schema_raw, bool)
        or not isinstance(schema_raw, int)
        or schema_raw != _STATE_SCHEMA_VERSION
    ):
        return None, "unsupported_task_state_schema"
    task_id = payload.get("task_id")
    objective = payload.get("objective")
    session_id = payload.get("session_id")
    sequence = payload.get("sequence")
    if not isinstance(task_id, str) or not task_id.strip():
        return None, "state_task_id_invalid"
    if not isinstance(objective, str) or not objective.strip():
        return None, "state_objective_invalid"
    if not isinstance(session_id, str) or not session_id.strip():
        return None, "state_session_id_invalid"
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        return None, "state_sequence_invalid"
    if task_id != make_task_id(session_id, sequence):
        return None, "state_task_id_inconsistent"
    origin = payload.get("origin", "user_instruction")
    if not isinstance(origin, str) or origin not in TASK_ORIGINS:
        return None, "state_origin_invalid"
    accepted_at = payload.get("accepted_at", "")
    if not isinstance(accepted_at, str):
        return None, "state_accepted_at_invalid"
    origin_event_id, valid = _optional_str(payload, "origin_event_id")
    if not valid:
        return None, "state_origin_event_id_invalid"
    resumed_from_session_id, valid = _optional_str(payload, "resumed_from_session_id")
    if not valid:
        return None, "state_resumed_from_session_id_invalid"
    request_id, valid = _optional_str(payload, "request_id")
    if not valid:
        return None, "state_request_id_invalid"
    parent_session_id, valid = _optional_str(payload, "parent_session_id")
    if not valid:
        return None, "state_parent_session_id_invalid"
    # An omitted list is the documented first-format default; a present
    # ``null`` (or any non-list) is a broken record, not an empty list.
    amendments = _string_list(payload.get("amendments", _MISSING))
    if amendments is None:
        return None, "state_amendments_invalid"
    prior_objectives = _string_list(
        payload.get("prior_objectives", _MISSING), limit=TASK_PRIOR_OBJECTIVES_MAX
    )
    if prior_objectives is None:
        return None, "state_prior_objectives_invalid"
    objective_truncated = payload.get("objective_truncated", False)
    if not isinstance(objective_truncated, bool):
        return None, "state_objective_truncated_invalid"
    state = SessionTaskState(
        task_id=task_id,
        objective=objective,
        session_id=session_id,
        sequence=sequence,
        origin=origin,
        origin_event_id=origin_event_id,
        accepted_at=accepted_at,
        amendments=amendments,
        prior_objectives=prior_objectives,
        resumed_from_session_id=resumed_from_session_id,
        request_id=request_id,
        parent_session_id=parent_session_id,
        objective_truncated=objective_truncated,
    )
    return state, ""


@dataclass(frozen=True)
class TaskTransition:
    """Outcome of offering an instruction to the task state machine."""

    kind: str  # accepted | replaced | amended | kept | ignored | deferred
    relation: str
    state: SessionTaskState | None
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.kind in {"accepted", "replaced", "amended"}


def normalize_task_relation(value: Any) -> tuple[str, bool]:
    """Return ``(relation, valid)``; unknown values fall back to ``auto``."""

    if value is None:
        return "auto", True
    text = str(value).strip().lower()
    if not text:
        return "auto", True
    if text in TASK_RELATIONS:
        return text, True
    return "auto", False


def validate_task_relation(value: Any) -> str:
    """Return the canonical relation or raise ``ValueError`` for unknown values.

    ``None``/empty means the caller supplied no relation (``auto``). Anything
    else must be one of :data:`TASK_RELATIONS`; an unknown value is a caller
    bug or a malformed external request and is never coerced into a task
    transition of any kind.
    """

    relation, valid = normalize_task_relation(value)
    if not valid:
        raise ValueError(
            f"unsupported task_relation {str(value)[:80]!r}; expected one of "
            + ", ".join(TASK_RELATIONS)
        )
    return relation


def instruction_is_task_candidate(
    instruction: str,
    *,
    provenance: str = "accepted_turn",
) -> bool:
    """Whether an instruction can carry task identity at all.

    Control commands are classified by *provenance*, not by the first
    character of accepted text: the chat command parser, native
    slash routes and the one-shot CLI decide what is a command before a turn
    exists, so an instruction that reaches the turn runtime
    (``accepted_turn``) is a request — including a path-first request such as
    ``/work/app.py: review the input validation``. Only host-managed context
    wrappers (text shaped like the host's own markers) are excluded there.

    ``legacy_log`` provenance is a ``user_message`` replayed from a log
    written before task state existed; nothing recorded what the host made of
    it, so control-shaped single lines (``/status``, ``:q``) are kept out of
    the objective as well.
    """

    from .prompt_context import _is_host_managed_user_context_message

    clean = str(instruction or "").strip()
    if not clean:
        return False
    if _is_host_managed_user_context_message(clean):
        return False
    if provenance == "legacy_log" and clean[:1] in {"/", ":"} and "\n" not in clean:
        return False
    return True


def _amendment_lines(text: str) -> list[str]:
    """The non-blank lines of an accepted amendment, exactly, in order.

    A derived, line-oriented view (membership queries, the bounded brief
    projection); repeats are kept and each line keeps its indentation,
    internal spacing and case. The amendment itself — blank lines and line
    endings included — is what the state stores.
    """

    return [line for line in str(text or "").splitlines() if line.strip()]


def _accepted_objective(instruction: str) -> tuple[str, bool]:
    """The objective text a state stores for an accepted instruction.

    Exactly the accepted text — whitespace, indentation, line structure and
    case included — for every ordinary request. Only a request beyond
    :data:`TASK_OBJECTIVE_MAX_CHARS` is cut, and that is recorded on the
    state (``objective_truncated``) rather than done silently.
    """

    text = str(instruction or "")
    if len(text) <= TASK_OBJECTIVE_MAX_CHARS:
        return text, False
    return text[:TASK_OBJECTIVE_MAX_CHARS], True


def make_task_id(session_id: str, sequence: int) -> str:
    return f"{session_id}:task:{sequence}"


def transition_task_state(
    current: SessionTaskState | None,
    *,
    instruction: str,
    relation: str,
    session_id: str,
    origin_event_id: str | None,
    origin: str = "user_instruction",
    now: str | None = None,
    request_id: str | None = None,
    parent_session_id: str | None = None,
    next_sequence: int | None = None,
    provenance: str = "accepted_turn",
    unrecovered: bool = False,
) -> TaskTransition:
    """Pure state machine: decide what an accepted instruction does to the task.

    Rules (deterministic, no language classification, no text matching):

    * no active task → the instruction becomes the task, whatever the relation;
    * the same accepted request offered again (equal, non-empty ``request_id``,
      e.g. a re-dispatched queued prompt after a provider failure or a restart)
      → the task is kept, whatever the relation;
    * ``new_task`` → replace, even when the text equals the current objective:
      identity follows accepted requests, never text; the old objective is
      kept as identity history;
    * ``amendment`` → the instruction's lines join the task's constraints;
    * ``continuation`` and ``auto`` → the objective is kept unchanged. ``auto``
      is the conservative default for callers without relation metadata: the
      current message is still delivered to the model as the user turn, it
      just never silently becomes the root objective.

    ``next_sequence`` is the sequence a newly accepted task receives; callers
    derive it from the session's persisted task events so an id is never
    reused after a clear or a resume. Without it the sequence continues from
    ``current`` (or starts at 1).

    ``unrecovered`` describes a session whose saved task state could not be
    read on resume: the previous objective is unknown, so ``auto`` and
    ``continuation`` input is delivered but *deferred* rather than quietly
    installed as a brand-new objective. Only an explicit ``new_task``
    establishes a task in that session.
    """

    relation = validate_task_relation(relation)
    if not instruction_is_task_candidate(instruction, provenance=provenance):
        return TaskTransition(kind="ignored", relation=relation, state=current, reason="not_a_task")
    objective, objective_truncated = _accepted_objective(instruction)
    accepted_at = now or _now_iso()
    clean_request_id = str(request_id or "").strip() or None

    if current is not None and clean_request_id and current.request_id == clean_request_id:
        return TaskTransition(kind="kept", relation=relation, state=current, reason="same_request")

    if current is None:
        if unrecovered and relation != "new_task":
            return TaskTransition(
                kind="deferred",
                relation=relation,
                state=None,
                reason="task_unrecovered_explicit_new_task_required",
            )
        sequence = max(1, int(next_sequence or 1))
        state = SessionTaskState(
            task_id=make_task_id(session_id, sequence),
            objective=objective,
            session_id=session_id,
            sequence=sequence,
            origin=origin,
            origin_event_id=origin_event_id,
            accepted_at=accepted_at,
            request_id=clean_request_id,
            parent_session_id=parent_session_id,
            objective_truncated=objective_truncated,
        )
        return TaskTransition(kind="accepted", relation=relation, state=state)

    if relation == "new_task":
        sequence = max(current.sequence + 1, int(next_sequence or 0))
        prior = (current.objective, *current.prior_objectives)[:TASK_PRIOR_OBJECTIVES_MAX]
        state = SessionTaskState(
            task_id=make_task_id(session_id, sequence),
            objective=objective,
            session_id=session_id,
            sequence=sequence,
            origin=origin,
            origin_event_id=origin_event_id,
            accepted_at=accepted_at,
            prior_objectives=prior,
            request_id=clean_request_id,
            parent_session_id=parent_session_id,
            objective_truncated=objective_truncated,
        )
        return TaskTransition(kind="replaced", relation=relation, state=state)

    if relation == "amendment":
        # The accepted amendment joins the task exactly as written: one unit,
        # with its line order, repeated lines, blank lines, indentation and
        # line endings. Repeated content *inside* an accepted input is not a
        # repeated request. Only an accepted input that is, as a whole, an
        # exact repeat of an existing amendment or of the objective is folded
        # (``src/Parser.py`` and ``src/parser.py`` are two amendments).
        text = str(instruction or "")
        if text == current.objective or tuple.__contains__(current.amendments, text):
            return TaskTransition(
                kind="kept", relation=relation, state=current, reason="no_new_constraint"
            )
        return TaskTransition(
            kind="amended",
            relation=relation,
            state=replace(current, amendments=AcceptedAmendments((text, *current.amendments))),
        )

    return TaskTransition(kind="kept", relation=relation, state=current, reason="objective_kept")


@dataclass(frozen=True)
class RecoveredTaskState:
    """Result of replaying a persisted session log for task identity."""

    state: SessionTaskState | None
    # event | legacy_user_events | none | unrecoverable
    recovery: str
    reason: str = ""
    user_message_events_seen: int = field(default=0)
    # ``session_id`` stamped on the log event the state was rebuilt from (the
    # writer of that log), so a caller can check the log belongs to the owner
    # it expects before installing the state.
    event_session_id: str | None = None
    # True when the log carries task-state-aware records (a ``session_start``
    # with ``task_state_schema``, task-state events, or user events carrying
    # input provenance). User messages written after such a record are never
    # interpreted through the legacy user-message fallback: a request without
    # an acceptance record was not accepted.
    modern_log: bool = False
    task_evidence_payload: dict[str, Any] | None = None
    task_evidence_session_id: str | None = None
    acknowledged_checkpoint: dict[str, Any] | None = None
    checkpoint_event_session_id: str | None = None


def _event_session(event: dict[str, Any]) -> str | None:
    raw = event.get("session_id")
    return str(raw).strip() or None if raw is not None else None


@dataclass
class _LatestTaskRecord:
    """What the newest task-state event after the last boundary asserts."""

    seen: bool = False
    # valid | none | unrecoverable
    outcome: str = "none"
    state: SessionTaskState | None = None
    reason: str = ""
    session_id: str | None = None
    # Set when the log declares an uncommitted record it cannot identify
    # (``UNCOMMITTED_BOUNDARY_EVENT`` with ``unidentified``): until the next
    # user-driven conversation boundary nothing in this stretch of the log can
    # be told apart from a record the store disowned, so the task stays
    # unrecoverable whatever later task-state events say.
    unidentified_uncommitted: bool = False

    def reset(self) -> None:
        self.seen = False
        self.outcome = "none"
        self.state = None
        self.reason = ""
        self.session_id = None
        self.unidentified_uncommitted = False

    def mark_unidentified_uncommitted(self, event: dict[str, Any]) -> None:
        self.seen = True
        self.unidentified_uncommitted = True
        self.outcome = "unrecoverable"
        self.state = None
        self.reason = "uncommitted_record_unidentified"
        self.session_id = _event_session(event) or self.session_id


def _read_task_state_event(event: dict[str, Any], record: _LatestTaskRecord) -> None:
    """Validate one ``session_task_state`` event into ``record``.

    Every event is an explicit assertion or it is invalid; nothing is skipped
    and nothing is guessed. The transition and the state must agree:

    * an active-state transition (``accepted``, ``replaced``, ``amended``,
      ``restored``) with a state that validates completely under the
      supported schema (:func:`validate_task_state_payload`) → the active task;
    * ``state: null`` with a retiring transition (``cleared``, ``restore_none``,
      ``rolled_back``) → no task;
    * ``state: null`` with an unrecovered transition (``restore_unrecoverable``,
      ``restore_refused``) → the limitation persists across further recoveries;
    * anything else — non-object payload, missing ``state``, unknown or
      malformed ``transition``/``relation``, an active state paired with a
      retiring/unrecovered/unknown transition, a null state with an
      active-state transition, unsupported schema, a state whose fields or
      identity do not validate — is a contradiction: unrecoverable, and the
      older records are not used as a fallback.

    A state carried over from another session (``origin: resumed``) keeps its
    original owner's ids; ownership is checked by the caller against the
    log writer (``RecoveredTaskState.event_session_id``), not here.
    """

    if record.unidentified_uncommitted:
        # The log cannot say which record it disowned; nothing after that
        # point is trustworthy until a user-driven boundary resets it.
        return
    record.seen = True
    record.session_id = _event_session(event)
    record.state = None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        record.outcome, record.reason = "unrecoverable", "latest_task_state_payload_invalid"
        return
    if "state" not in payload:
        record.outcome, record.reason = "unrecoverable", "latest_task_state_missing_state"
        return
    transition_raw = payload.get("transition")
    transition = transition_raw.strip() if isinstance(transition_raw, str) else ""
    if transition not in _KNOWN_TRANSITIONS:
        record.outcome = "unrecoverable"
        record.reason = "latest_task_state_transition_unknown"
        return
    relation_raw = payload.get("relation")
    if relation_raw is not None and (
        not isinstance(relation_raw, str) or relation_raw.strip() not in _TRANSITION_RELATIONS
    ):
        record.outcome, record.reason = "unrecoverable", "latest_task_state_relation_invalid"
        return
    state_payload = payload.get("state")
    if state_payload is None:
        if transition in _NO_TASK_TRANSITIONS:
            record.outcome, record.reason = "none", "task_state_cleared"
        elif transition in _UNRECOVERED_TRANSITIONS:
            saved_reason = str(payload.get("reason") or "").strip()
            record.outcome = "unrecoverable"
            record.reason = f"{transition}_persisted" + (f":{saved_reason}" if saved_reason else "")
        else:
            # An active-state transition that carries no state.
            record.outcome = "unrecoverable"
            record.reason = "latest_task_state_missing_active_state"
        return
    if transition not in _ACTIVE_STATE_TRANSITIONS:
        # ``cleared`` / ``restore_*`` / ``rolled_back`` paired with a state
        # object: the record contradicts itself and asserts nothing usable.
        record.outcome = "unrecoverable"
        record.reason = f"latest_task_state_transition_incompatible:{transition}"
        return
    parsed, reason = validate_task_state_payload(state_payload)
    if parsed is None:
        record.outcome = "unrecoverable"
        record.reason = (
            "unsupported_task_state_schema"
            if reason == "unsupported_task_state_schema"
            else f"latest_task_state_event_invalid:{reason}"
        )
        return
    record.outcome, record.state, record.reason = "valid", parsed, ""


def recover_task_state_from_events(
    events: Any,
    *,
    session_id: str | None = None,
) -> RecoveredTaskState:
    """Deterministically rebuild task identity from a session's event log.

    Precedence after the last conversation boundary:

    1. the latest persisted ``session_task_state`` event (authoritative) —
       an active task, an explicit "no task", or a persisted unrecoverable /
       refused outcome; an event that cannot be validated is reported as
       ``unrecoverable`` and never replaced by an older record or a guess;
    2. for logs written before task state existed (and only those), the
       first task-candidate ``user_message`` event;
    3. nothing: no task was ever accepted.

    A user message written by a task-state-aware runtime (after a
    ``session_start`` carrying ``task_state_schema``, or carrying its own
    ``provenance``) without an acceptance record describes a request that
    was not accepted (a failed acceptance, a conversational-only turn); it is
    never promoted to a task. Messages that precede every such marker were
    written before task state existed and keep the legacy rule, so a legacy
    log resumed by a modern runtime (which appends its own ``session_start``)
    still recovers its first request. No tool result, summary, or assistant
    text is ever selected as the task.
    """

    latest = _LatestTaskRecord()
    first_user_instruction: str | None = None
    first_user_event_id: str | None = None
    first_user_event_session: str | None = None
    user_events_seen = 0
    modern_log = False
    task_evidence_payload = None
    task_evidence_session_id = None
    acknowledged_checkpoint = None
    checkpoint_event_session_id = None

    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        payload = event.get("payload")
        if event_type == "anytime_checkpoint":
            if (
                isinstance(payload, dict)
                and payload.get("status") == "verified_checkpoint_preserved"
            ):
                acknowledged_checkpoint = payload.get("best")
                checkpoint_event_session_id = _event_session(event)
            continue
        if event_type == "session_task_evidence":
            task_evidence_payload = payload if isinstance(payload, dict) else None
            task_evidence_session_id = _event_session(event)
            continue
        if event_type == "tool_call" and task_evidence_payload is not None:
            # A crash between dispatch and recording its observed edits must
            # not resurrect an earlier, apparently clean preservation ledger.
            task_evidence_payload = {**task_evidence_payload, "baseline_available": False}
        if event_type == "session_start":
            if isinstance(payload, dict) and payload.get("task_state_schema") is not None:
                modern_log = True
            continue
        if event_type == "task_identity_resolved":
            modern_log = True
            continue
        if event_type in TASK_BOUNDARY_EVENTS:
            trigger = str(payload.get("trigger") or "") if isinstance(payload, dict) else ""
            if trigger and trigger not in TASK_BOUNDARY_TRIGGERS:
                continue
            # A user-driven clear ends whatever task was active: records and
            # legacy candidates before it no longer count. (A task-state-aware
            # runtime persists the retirement itself, before this boundary.)
            latest.reset()
            first_user_instruction = None
            first_user_event_id = None
            first_user_event_session = None
            task_evidence_payload = None
            task_evidence_session_id = None
            acknowledged_checkpoint = None
            checkpoint_event_session_id = None
            continue
        if event_type == TASK_STATE_EVENT:
            modern_log = True
            _read_task_state_event(event, latest)
            continue
        if event_type == UNCOMMITTED_BOUNDARY_EVENT:
            unidentified = isinstance(payload, dict) and bool(payload.get("unidentified"))
            if unidentified:
                modern_log = True
                latest.mark_unidentified_uncommitted(event)
            continue
        if event_type == "user_message":
            user_events_seen += 1
            if not isinstance(payload, dict):
                continue
            provenance = payload.get("provenance")
            if isinstance(provenance, dict) or modern_log:
                # Written by a task-state-aware runtime: the acceptance
                # record, not the message text, says whether this became a
                # task.
                modern_log = True
                continue
            if first_user_instruction is not None:
                continue
            content = payload.get("content")
            if not isinstance(content, str):
                content = payload.get("display_content")
            if isinstance(content, str) and instruction_is_task_candidate(
                content, provenance="legacy_log"
            ):
                first_user_instruction = content
                event_id = event.get("event_id")
                first_user_event_id = str(event_id).strip() if event_id else None
                first_user_event_session = _event_session(event)

    if latest.seen:
        if latest.outcome == "unrecoverable":
            return RecoveredTaskState(
                state=None,
                recovery="unrecoverable",
                reason=latest.reason,
                user_message_events_seen=user_events_seen,
                event_session_id=latest.session_id,
                modern_log=modern_log,
            )
        if latest.outcome == "none":
            return RecoveredTaskState(
                state=None,
                recovery="none",
                reason=latest.reason or "task_state_cleared",
                user_message_events_seen=user_events_seen,
                event_session_id=latest.session_id,
                modern_log=modern_log,
            )
        return RecoveredTaskState(
            state=latest.state,
            recovery="event",
            task_evidence_payload=task_evidence_payload,
            task_evidence_session_id=task_evidence_session_id,
            acknowledged_checkpoint=acknowledged_checkpoint,
            checkpoint_event_session_id=checkpoint_event_session_id,
            user_message_events_seen=user_events_seen,
            event_session_id=latest.session_id,
            modern_log=modern_log,
        )

    if first_user_instruction is None and modern_log:
        return RecoveredTaskState(
            state=None,
            recovery="none",
            reason="no_task_accepted",
            user_message_events_seen=user_events_seen,
            modern_log=True,
        )

    if first_user_instruction is not None:
        owner = str(session_id or "").strip() or "legacy-session"
        objective, objective_truncated = _accepted_objective(first_user_instruction)
        state = SessionTaskState(
            task_id=make_task_id(owner, 1),
            objective=objective,
            session_id=owner,
            sequence=1,
            origin="recovered_legacy_user_events",
            origin_event_id=first_user_event_id,
            accepted_at="",
            objective_truncated=objective_truncated,
        )
        return RecoveredTaskState(
            state=state,
            recovery="legacy_user_events",
            user_message_events_seen=user_events_seen,
            event_session_id=first_user_event_session,
            modern_log=modern_log,
        )

    return RecoveredTaskState(
        state=None,
        recovery="none",
        reason="no_task_events",
        user_message_events_seen=user_events_seen,
    )


# ---------------------------------------------------------------------------
# Session-level operations (the host transitions)
# ---------------------------------------------------------------------------


class TaskPersistenceError(RuntimeError):
    """The session log refused to record a task transition.

    Raised before the in-memory task state changes, so a caller that sees it
    knows the runtime holds exactly the task it held before: nothing was
    accepted, and nothing may be dispatched as if it had been.
    """


def _session_id_of(session: Any) -> str:
    store = getattr(session, "store", None)
    return str(getattr(store, "session_id", "") or "").strip() or "session"


def _persist_task_transition(
    session: Any,
    *,
    transition: str,
    relation: str,
    state: SessionTaskState | None,
    reason: str = "",
    extra: dict[str, Any] | None = None,
) -> str | None:
    """Append the transition to the session log; returns the event id.

    Sessions without an event store (lightweight test doubles) keep identity
    in memory only and return ``None``. A store that raises turns into
    :class:`TaskPersistenceError` so callers can refuse to proceed instead of
    treating an unrecorded acceptance as durable.
    """

    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    if not callable(append):
        return None
    payload: dict[str, Any] = {
        "transition": transition,
        "relation": relation,
        "state": state.to_payload() if state is not None else None,
    }
    if reason:
        payload["reason"] = reason
    if extra:
        payload.update(extra)
    try:
        event_id = append(TASK_STATE_EVENT, payload)
    except Exception as exc:  # noqa: BLE001 - re-raised with the task lifecycle meaning
        raise TaskPersistenceError(
            f"could not persist task transition {transition!r}: {exc}"
        ) from exc
    return str(event_id).strip() if isinstance(event_id, str) and event_id.strip() else None


def _rerender_task_brief(session: Any) -> None:
    from .prompt_context import refresh_session_task_brief_message

    refresh_session_task_brief_message(session)


def _persisted_task_sequence_high_water(session: Any) -> int:
    """Highest task sequence ever recorded for this session's own identity.

    Read from the session's persisted ``session_task_state`` events (the same
    log a resume replays), so a task accepted after ``/clear`` or after a
    resume can never reuse an id that already appeared in the log. States
    carried over from another session keep that session's ids and are not
    part of this session's numbering.
    """

    own = _session_id_of(session)
    store = getattr(session, "store", None)
    events: list[Any] = []
    events_since = getattr(store, "events_since", None)
    if callable(events_since):
        try:
            # Read-only slice of the store's own list: no JSON round trip.
            since = events_since(0)
            events = list(since[0]) if isinstance(since, tuple) and since else []
        except Exception:  # noqa: BLE001 - fall back to the copying accessor below
            events = []
    if not events:
        snapshot = getattr(store, "events_snapshot", None)
        if callable(snapshot):
            try:
                copied = snapshot()
                events = list(copied) if isinstance(copied, list) else []
            except Exception:  # noqa: BLE001 - the in-memory mark still applies
                events = []
    highest = 0
    for event in events:
        if not isinstance(event, dict) or str(event.get("type") or "") != TASK_STATE_EVENT:
            continue
        payload = event.get("payload")
        state = payload.get("state") if isinstance(payload, dict) else None
        if not isinstance(state, dict) or str(state.get("session_id") or "").strip() != own:
            continue
        sequence = state.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            highest = max(highest, sequence)
    return highest


def _task_sequence_mark(session: Any) -> int:
    mark = getattr(session, "task_sequence_high_water", 0)
    return mark if isinstance(mark, int) and not isinstance(mark, bool) else 0


def _remember_task_sequence(session: Any, state: SessionTaskState | None) -> None:
    if state is None or state.session_id != _session_id_of(session):
        return
    try:
        session.task_sequence_high_water = max(_task_sequence_mark(session), state.sequence)
    except Exception:  # noqa: BLE001 - immutable fakes fall back to the persisted scan
        pass


def next_task_sequence(session: Any, current: SessionTaskState | None = None) -> int:
    """The sequence the next newly accepted task of this session must use."""

    highest = max(_persisted_task_sequence_high_water(session), _task_sequence_mark(session))
    if current is not None and current.session_id == _session_id_of(session):
        highest = max(highest, current.sequence)
    return highest + 1


def _set_unrecovered_flag(session: Any, value: bool) -> None:
    try:
        session.task_state_unrecovered = value
    except Exception:  # noqa: BLE001 - fake sessions may be immutable
        pass


def accept_session_task(
    session: Any,
    *,
    instruction: str,
    relation: str | None = None,
    origin_event_id: str | None = None,
    request_id: str | None = None,
) -> TaskTransition:
    """Offer a host-accepted turn instruction to the session's task state.

    Called by the turn runtime after the ``user_message`` event is recorded and
    before any model request. A transition is persisted first and installed in
    ``session.task_state`` only once the log accepted it, so memory, log, and
    the pinned brief never disagree about what was accepted: a persistence
    failure raises :class:`TaskPersistenceError` and leaves the previous task
    untouched. The brief is re-rendered so the first request already carries
    the accepted objective.

    ``request_id`` is the host's identity for the accepted request (a queued
    prompt id). Offering the same request again keeps the task it created,
    which is how a retry after a provider failure or a restart stays on the
    same task without any text comparison.

    The instruction is ``accepted_turn`` input by construction (the caller is
    the turn runtime), so its text is a task candidate whatever it starts
    with. In a session whose saved task could not be recovered, ``auto`` /
    ``continuation`` input is delivered but deferred (the unrecovered
    limitation stays in force); only ``new_task`` establishes a task there.
    """

    resolved_relation = validate_task_relation(relation)
    origin = (
        "delegated" if int(getattr(session, "subagent_depth", 0) or 0) > 0 else "user_instruction"
    )
    current = getattr(session, "task_state", None)
    if current is not None and not isinstance(current, SessionTaskState):
        current = None
    parent_session_id = getattr(session, "task_parent_session_id", None)
    parent_session_id = str(parent_session_id or "").strip() or None
    needs_new_identity = current is None or resolved_relation == "new_task"
    transition = transition_task_state(
        current,
        instruction=instruction,
        relation=resolved_relation,
        session_id=_session_id_of(session),
        origin_event_id=origin_event_id,
        origin=origin,
        request_id=request_id,
        parent_session_id=parent_session_id,
        next_sequence=next_task_sequence(session, current) if needs_new_identity else None,
        provenance="accepted_turn",
        unrecovered=bool(getattr(session, "task_state_unrecovered", False)),
    )
    if transition.changed:
        _persist_task_transition(
            session,
            transition=transition.kind,
            relation=transition.relation,
            state=transition.state,
        )
        session.task_state = transition.state
        _remember_task_sequence(session, transition.state)
        # A newly accepted task supersedes any unrecovered-resume limitation.
        _set_unrecovered_flag(session, False)
    _rerender_task_brief(session)
    return transition


def clear_session_task(session: Any, *, reason: str) -> bool:
    """Deliberate host transition: retire the active task (e.g. ``/clear``).

    The retirement is persisted before memory changes; the retired task's
    sequence stays reserved so a later task never reuses its id.
    """

    current = getattr(session, "task_state", None)
    had_task = isinstance(current, SessionTaskState)
    if had_task:
        _remember_task_sequence(session, current)
        _persist_task_transition(
            session, transition="cleared", relation="host", state=None, reason=reason
        )
    session.task_state = None
    session._task_evidence_state = None
    session._acknowledged_checkpoint = None
    _set_unrecovered_flag(session, False)
    _rerender_task_brief(session)
    return had_task


def restore_session_task_state(
    session: Any,
    recovered: RecoveredTaskState,
    *,
    source_session_id: str | None = None,
    expected_session_id: str | None = None,
    expected_parent_session_id: str | None = None,
) -> SessionTaskState | None:
    """Install task identity recovered from a persisted log into a session.

    The task keeps its original ``task_id`` and objective. When the log belongs
    to a different session than the one being populated, the carried-over
    origin is recorded so the state never claims to have been accepted here.

    ``expected_session_id`` / ``expected_parent_session_id`` let a caller that
    knows which owner the log must belong to (a resumed child and its parent)
    refuse saved state written for someone else: on a mismatch nothing is
    installed, the refusal is persisted, and the session reports the state as
    unrecovered instead of adopting a task that is not its own.
    """

    state = recovered.state
    own_session_id = _session_id_of(session)
    refusal = ""
    if state is not None:
        # The log writer stamped on the recovered event is the owner of the
        # saved state; a state carried over from another session deliberately
        # keeps that session's ids, so the state's own ``session_id`` is not
        # the ownership check. Logs without a writer stamp cannot be checked.
        log_owner = recovered.event_session_id
        if expected_session_id and log_owner and log_owner != expected_session_id:
            refusal = "session_id_mismatch"
        elif (
            expected_parent_session_id
            and state.parent_session_id
            and state.parent_session_id != expected_parent_session_id
        ):
            refusal = "parent_session_id_mismatch"
    if refusal:
        _persist_task_transition(
            session,
            transition="restore_refused",
            relation="host",
            state=None,
            reason=refusal,
            extra={
                "recovery": recovered.recovery,
                "source_session_id": source_session_id,
                "expected_session_id": expected_session_id,
                "expected_parent_session_id": expected_parent_session_id,
                "saved_session_id": recovered.event_session_id,
                "saved_task_id": state.task_id if state is not None else None,
                "saved_parent_session_id": state.parent_session_id if state is not None else None,
            },
        )
        session.task_state = None
        session._task_evidence_state = None
        session._acknowledged_checkpoint = None
        _set_unrecovered_flag(session, True)
        _rerender_task_brief(session)
        return None
    if state is not None and source_session_id and source_session_id != own_session_id:
        state = replace(state, origin="resumed", resumed_from_session_id=source_session_id)
    _persist_task_transition(
        session,
        transition="restored" if state is not None else f"restore_{recovered.recovery}",
        relation="host",
        state=state,
        reason=recovered.reason,
        extra={
            "recovery": recovered.recovery,
            "source_session_id": source_session_id,
        },
    )
    session.task_state = state
    _remember_task_sequence(session, state)
    from .task_evidence import restore_task_evidence

    restore_task_evidence(
        session,
        recovered.task_evidence_payload,
        event_session_id=recovered.task_evidence_session_id,
        expected_session_id=expected_session_id or source_session_id or recovered.event_session_id,
    )
    session._acknowledged_checkpoint = None
    checkpoint = recovered.acknowledged_checkpoint
    expected_writer = expected_session_id or source_session_id or recovered.event_session_id
    if (
        state is not None
        and isinstance(checkpoint, dict)
        and checkpoint.get("task_id") == state.task_id
        and recovered.checkpoint_event_session_id
        and (not expected_writer or recovered.checkpoint_event_session_id == expected_writer)
    ):
        try:
            session.store.append(
                "anytime_checkpoint",
                {
                    "status": "verified_checkpoint_preserved",
                    "best": checkpoint,
                    "restored": True,
                },
            )
            session._acknowledged_checkpoint = dict(checkpoint)
        except Exception:
            pass
    _set_unrecovered_flag(session, recovered.recovery == "unrecoverable")
    _rerender_task_brief(session)
    return state


__all__ = [
    "AcceptedAmendments",
    "RecoveredTaskState",
    "SessionTaskState",
    "TASK_BOUNDARY_EVENTS",
    "TASK_ORIGINS",
    "TASK_RELATIONS",
    "TASK_STATE_EVENT",
    "TASK_STATE_SCHEMA_VERSION",
    "UNCOMMITTED_BOUNDARY_EVENT",
    "TaskPersistenceError",
    "TaskTransition",
    "accept_session_task",
    "clear_session_task",
    "instruction_is_task_candidate",
    "make_task_id",
    "next_task_sequence",
    "normalize_task_relation",
    "recover_task_state_from_events",
    "restore_session_task_state",
    "transition_task_state",
    "validate_task_relation",
    "validate_task_state_payload",
]
