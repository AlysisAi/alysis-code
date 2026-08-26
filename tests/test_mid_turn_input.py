from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from alysis_code.agent.steering import (
    MAX_PENDING_STEER_MESSAGES,
    MAX_STEER_MESSAGE_CHARS,
    SteerInbox,
    build_steer_messages,
    steer_inbox_for,
)
from alysis_code.cli_impl.chat.mid_turn_policy import (
    MidTurnAction,
    block_message,
    classify_mid_turn,
    defer_message,
    is_command,
)

READ_ONLY_COMMANDS = [
    "/help",
    "/status",
    "/pwd",
    "/context",
    "/ctx",
    "/usage",
    "/usage hud on",
    "/model-info",
    "/model-info gpt-5",
    "/trace full",
    "/toolbar reset",
    "/images",
    "/image screenshot.png",
    "/paste-image",
    "/clear-images",
    "/terminals list",
    "/terminals kill 42",
    "/subagents",
]

TURN_MUTATING_COMMANDS = [
    "/clear",
    "/resume",
    "/compact",
    "/plan draft x",
    "/ask what is this",
    "/subagent explorer find x",
    "/forge",
    ":forge",
    "/login",
    "/logout",
    "/stream off",
    "/report",
    "/feedback",
    "/assets",
]

DEFERRED_COMMANDS = [
    "/model",
    "/model gpt-5",
    "/mode",
    "/mode auto",
    "/persona",
    "/persona architect",
    "/config",
    "/config set foo bar",
]


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_commands_run_mid_turn(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.ALLOW


@pytest.mark.parametrize("command", TURN_MUTATING_COMMANDS)
def test_turn_mutating_commands_are_blocked_mid_turn(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.BLOCK


@pytest.mark.parametrize("command", DEFERRED_COMMANDS)
def test_safe_configuration_commands_are_deferred_mid_turn(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.DEFER
    assert command.split(maxsplit=1)[0].lower() in defer_message(command)
    assert "when this turn finishes" in defer_message(command)


@pytest.mark.parametrize("text", ["exit", "quit", "/exit", "/quit", ":q", "  EXIT  "])
def test_exit_words_are_blocked_not_delivered_to_the_model(text: str) -> None:
    assert classify_mid_turn(text) is MidTurnAction.BLOCK
    assert "Esc" in block_message(text)


@pytest.mark.parametrize(
    "text", ["actually use pytest", "no, the other file", "stop and explain", ""]
)
def test_prose_is_delivered_as_a_message(text: str) -> None:
    assert classify_mid_turn(text) is MidTurnAction.MESSAGE


def test_bare_slash_remains_a_safe_command() -> None:
    assert classify_mid_turn("/") is MidTurnAction.ALLOW


def test_unknown_commands_fail_closed() -> None:
    assert classify_mid_turn("/some-command-added-next-year") is MidTurnAction.BLOCK
    assert classify_mid_turn(":unknown --force") is MidTurnAction.BLOCK


def test_bare_only_commands_split_on_arguments() -> None:
    assert classify_mid_turn("/skill") is MidTurnAction.ALLOW
    assert classify_mid_turn("/skill reviewer audit this") is MidTurnAction.BLOCK


def test_classification_is_case_and_whitespace_insensitive() -> None:
    assert classify_mid_turn("  /HELP  ") is MidTurnAction.ALLOW
    assert classify_mid_turn("  /MoDeL gpt-5 ") is MidTurnAction.DEFER


@pytest.mark.parametrize(
    "command", ["/resume", "/resume abc123", "/login", "/logout", "/clear", "/compact"]
)
def test_session_replacing_commands_stay_blocked(command: str) -> None:
    assert classify_mid_turn(command) is MidTurnAction.BLOCK


@pytest.mark.parametrize("command", [*TURN_MUTATING_COMMANDS, "/unknown", "exit"])
def test_every_block_message_explains_and_names_escape_hatch(command: str) -> None:
    message = block_message(command)
    assert message.strip()
    assert "Esc to interrupt" in message
    assert "wait for the turn to finish" in message


def test_is_command_distinguishes_commands_from_prose() -> None:
    assert is_command("/help")
    assert is_command(":forge")
    assert is_command("exit")
    assert not is_command("please fix the parser")
    assert not is_command("")


def test_drain_is_exactly_once() -> None:
    inbox = SteerInbox()
    inbox.send("first")
    inbox.send("second")

    assert inbox.drain() == ["first", "second"]
    assert inbox.drain() == []


def test_blank_messages_are_ignored() -> None:
    inbox = SteerInbox()

    assert inbox.send("") == ""
    assert inbox.send("  \n  ") == ""
    assert inbox.pending_count() == 0


def test_long_messages_are_truncated_with_visible_marker() -> None:
    inbox = SteerInbox()

    sent = inbox.send("x" * (MAX_STEER_MESSAGE_CHARS + 1000))

    assert sent.endswith(" [truncated]")
    assert len(sent) <= MAX_STEER_MESSAGE_CHARS
    assert inbox.drain() == [sent]


def test_queue_bound_keeps_newest_and_counts_evictions() -> None:
    inbox = SteerInbox()
    overflow = 5

    for index in range(MAX_PENDING_STEER_MESSAGES + overflow):
        inbox.send(f"m{index}")

    drained = inbox.drain()
    assert len(drained) == MAX_PENDING_STEER_MESSAGES
    assert drained[0] == f"m{overflow}"
    assert drained[-1] == f"m{MAX_PENDING_STEER_MESSAGES + overflow - 1}"
    assert inbox.dropped_count() == overflow


def test_restore_front_preserves_chronology_with_newer_arrivals() -> None:
    inbox = SteerInbox()
    inbox.send("older first")
    inbox.send("older second")
    drained = inbox.drain()

    inbox.send("newer arrival")
    inbox.restore_front(drained)

    assert inbox.drain() == ["older first", "older second", "newer arrival"]


def test_restore_front_keeps_newest_messages_when_combined_queue_is_full() -> None:
    inbox = SteerInbox()
    inbox.send("oldest restored")
    drained = inbox.drain()
    for index in range(MAX_PENDING_STEER_MESSAGES):
        inbox.send(f"newer {index}")

    inbox.restore_front(drained)

    assert inbox.drain() == [f"newer {index}" for index in range(MAX_PENDING_STEER_MESSAGES)]
    assert inbox.dropped_count() == 1


def _exercise_concurrent_send_and_drain(total: int) -> tuple[list[str], int]:
    inbox = SteerInbox()
    collected: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        for index in range(total):
            inbox.send(f"msg{index}")
            if index % 17 == 0:
                time.sleep(0)

    def reader() -> None:
        while not stop.is_set():
            collected.extend(inbox.drain())
            time.sleep(0)
        collected.extend(inbox.drain())

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join()
    stop.set()
    reader_thread.join()
    return collected, inbox.dropped_count()


def test_concurrent_send_and_drain_conserve_every_message() -> None:
    total = 300

    for _ in range(20):
        collected, dropped = _exercise_concurrent_send_and_drain(total)
        assert len(set(collected)) == len(collected)
        assert len(collected) + dropped == total
        order = [int(message[3:]) for message in collected]
        assert order == sorted(order)


def test_inbox_attaches_lazily_and_idempotently() -> None:
    class Session:
        pass

    session = Session()
    assert steer_inbox_for(session) is None

    created = steer_inbox_for(session, create=True)
    assert created is not None
    assert steer_inbox_for(session) is created
    assert steer_inbox_for(session, create=True) is created


def test_sessions_that_cannot_take_an_attribute_opt_out() -> None:
    class SlottedSession:
        __slots__ = ()

    assert steer_inbox_for(SlottedSession(), create=True) is None


def test_steer_messages_are_durable_user_messages() -> None:
    built = build_steer_messages(["use pytest", "  ", "add type hints"])

    assert built == [
        {
            "role": "user",
            "content": "[Mid-turn message from the user] use pytest",
        },
        {
            "role": "user",
            "content": "[Mid-turn message from the user] add type hints",
        },
    ]


def test_status_line_describes_steer_queue_and_waiting_messages() -> None:
    from alysis_code.cli_impl.tui.app import _status_line_fragments

    def plain(fragments: list[tuple[str, str]]) -> str:
        return "".join(text for _style, text in fragments)

    assert "Esc or Ctrl+C to interrupt" in plain(_status_line_fragments(running=True))
    pending = plain(_status_line_fragments(running=True, input_pending=True))
    assert "Enter to steer" in pending
    assert "Ctrl+Q to queue" in pending
    queued = plain(_status_line_fragments(running=True, queued_count=2))
    assert "2 queued messages" in queued
    staged = plain(_status_line_fragments(running=True, staged_count=1))
    assert "1 staged command" in staged
    mixed = plain(_status_line_fragments(running=True, queued_count=2, staged_count=1))
    assert "1 staged command" in mixed
    assert "2 queued messages" in mixed


def test_pending_command_staging_is_bounded_and_preserves_every_command() -> None:
    from alysis_code.cli_impl.tui.app import (
        _MAX_PENDING_COMMANDS,
        _DeferredOperationKind,
        _stage_pending_command,
    )

    pending: list[Any] = []
    for index in range(_MAX_PENDING_COMMANDS):
        assert _stage_pending_command(pending, f"/verb-{index} value")

    assert not _stage_pending_command(pending, "/one-too-many value")
    assert len(pending) == _MAX_PENDING_COMMANDS
    assert all(operation.kind is _DeferredOperationKind.COMMAND for operation in pending)
    assert [operation.text for operation in pending] == [
        f"/verb-{index} value" for index in range(_MAX_PENDING_COMMANDS)
    ]


def test_independent_config_commands_are_not_coalesced() -> None:
    from alysis_code.cli_impl.tui.app import _stage_pending_command

    pending: list[Any] = []
    assert _stage_pending_command(pending, "/config set model model-a")
    assert _stage_pending_command(pending, "/config set default_mode auto")

    assert [operation.text for operation in pending] == [
        "/config set model model-a",
        "/config set default_mode auto",
    ]


def _run_live_tui(
    *,
    session_builder: Any,
    feed: Any,
    command_runner: Any | None = None,
    persona_cycle: Any | None = None,
) -> tuple[Any, list[tuple[str, str]]]:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    with create_pipe_input() as pipe:
        feed_errors: list[BaseException] = []

        def run_feed() -> None:
            try:
                feed(pipe)
            except BaseException as exc:  # noqa: BLE001 - surface feeder failures in the test
                feed_errors.append(exc)
                pipe.send_text("\x03")
                time.sleep(0.05)
                pipe.send_text("\x03")

        feeder = threading.Thread(target=run_feed, daemon=True)
        feeder.start()
        result, transcript = run_tui(
            TuiState(model_name="test-model", username="tester"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=session_builder,
            command_runner=command_runner,
            persona_cycle=persona_cycle,
            background_turns=True,
        )
        feeder.join(timeout=2)

    assert not feeder.is_alive()
    assert feed_errors == []
    return result, transcript


def test_read_only_command_runs_while_turn_is_active() -> None:
    started = threading.Event()
    release = threading.Event()
    status_seen = threading.Event()
    finished = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            started.set()
            assert release.wait(timeout=3)
            finished.set()
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        if text.strip() == "/status":
            status_seen.set()
            return "handled", "STATUS DURING TURN", None, None
        if text.strip() == "/exit":
            return "exit", "", None, None
        return "run", "", text, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("/status\r")
        assert status_seen.wait(timeout=2)
        release.set()
        assert finished.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert calls == ["work"]
    assert any(role == "system" and "STATUS DURING TURN" in text for role, text in transcript)


def test_allowed_command_cannot_start_a_second_concurrent_turn() -> None:
    started = threading.Event()
    command_seen = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            started.set()
            assert release.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        if text.strip() == "/status":
            command_seen.set()
            return "run", "", "must-not-start", {}
        if text.strip() == "/exit":
            return "exit", "", None, None
        return "run", "", text, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("/status\r")
        assert command_seen.wait(timeout=2)
        release.set()
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    _result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert calls == ["work"]
    assert any("already running" in text for role, text in transcript if role == "warn")


def test_deferred_commands_apply_in_submission_order_before_queued_turn() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    queued_started = threading.Event()
    events: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            events.append(f"turn:{text}")
            if text == "work":
                first_started.set()
                assert release_first.wait(timeout=3)
            elif text == "queued follow-up":
                queued_started.set()
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        events.append(f"command:{stripped}")
        if stripped == "/exit":
            return "exit", "", None, None
        if stripped.startswith("/"):
            return "handled", f"APPLIED {stripped}", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("/mode fast\r")
        pipe.send_text("/model gpt-5\r")
        pipe.send_text("/mode auto\r")
        pipe.send_text("queued follow-up\x11")
        time.sleep(0.1)
        release_first.set()
        assert queued_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert events.index("command:/mode fast") < events.index("command:/model gpt-5")
    assert events.index("command:/model gpt-5") < events.index("command:/mode auto")
    assert events.index("command:/mode auto") < events.index("turn:queued follow-up")
    assert transcript.count(("user", "/mode fast")) == 1
    assert transcript.count(("user", "/mode auto")) == 1
    assert transcript.count(("user", "/model gpt-5")) == 1
    assert any("when this turn finishes" in text for role, text in transcript if role == "system")


def test_deferred_command_cannot_start_a_turn() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    deferred_seen = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            first_started.set()
            assert release_first.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/mode auto":
            deferred_seen.set()
            return "run", "", "must-not-start", {}
        if stripped == "/exit":
            return "exit", "", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("/mode auto\r")
        time.sleep(0.05)
        release_first.set()
        assert deferred_seen.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/exit"
    assert calls == ["work"]
    assert any(
        "Deferred command was rejected" in text for role, text in transcript if role == "warn"
    )


def test_deferred_exit_stops_before_a_queued_turn_starts() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            first_started.set()
            assert release_first.wait(timeout=3)
            return 0

    def command_runner(_session: Any, text: str, _width: int) -> tuple[Any, ...]:
        stripped = text.strip()
        if stripped == "/config set base_url https://new.example/v1":
            return "exit", "Connection changed; restart required.", None, None
        return "run", "", stripped, {}

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("/config set base_url https://new.example/v1\r")
        pipe.send_text("must not start\x11")
        time.sleep(0.05)
        release_first.set()

    result, transcript = _run_live_tui(
        session_builder=BlockingSession,
        command_runner=command_runner,
        feed=feed,
    )

    assert result == "/config set base_url https://new.example/v1"
    assert calls == ["work"]
    assert any(
        "Discarded 1 pending item because this session is closing" in text
        for role, text in transcript
        if role == "warn"
    )


def test_enter_steers_the_running_turn() -> None:
    started = threading.Event()
    received = threading.Event()
    calls: list[str] = []
    notes: list[str] = []

    class SteeringSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                inbox = steer_inbox_for(self)
                if inbox is not None and inbox.pending_count():
                    notes.extend(inbox.drain())
                    received.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("steering message was not delivered")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("use the parser instead\r")
        assert received.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=SteeringSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work"]
    assert notes == ["use the parser instead"]
    assert ("user", "use the parser instead") in transcript
    assert any("Sent to the running turn" in text for role, text in transcript if role == "system")


def test_tui_echoes_the_truncated_text_that_was_actually_steered() -> None:
    started = threading.Event()
    received = threading.Event()
    notes: list[str] = []

    class SteeringSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = text, cancellation_token
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                inbox = steer_inbox_for(self)
                if inbox is not None and inbox.pending_count():
                    notes.extend(inbox.drain())
                    received.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("steering message was not delivered")

    oversized = "x" * (MAX_STEER_MESSAGE_CHARS + 500)

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text(oversized + "\r")
        assert received.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    _result, transcript = _run_live_tui(session_builder=SteeringSession, feed=feed)

    assert len(notes) == 1
    assert len(notes[0]) <= MAX_STEER_MESSAGE_CHARS
    assert notes[0].endswith(" [truncated]")
    assert ("user", notes[0]) in transcript
    assert ("user", oversized) not in transcript


def test_ctrl_q_queues_follow_up_in_order() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    third_started = threading.Event()
    calls: list[str] = []

    class QueueSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            if len(calls) == 1:
                first_started.set()
                assert release_first.wait(timeout=3)
            elif len(calls) == 3:
                third_started.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("first\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("second\x11")
        pipe.send_text("third\x11")
        time.sleep(0.05)
        release_first.set()
        assert third_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=QueueSession, feed=feed)

    assert result == "/exit"
    assert calls == ["first", "second", "third"]
    assert any("Queued - runs when this turn finishes" in text for _role, text in transcript)
    assert any("Running queued message" in text for _role, text in transcript)


def test_ctrl_q_queue_warns_when_full() -> None:
    started = threading.Event()
    cancelled = threading.Event()
    calls: list[str] = []

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            calls.append(text)
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    cancelled.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("turn was not cancelled")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        for index in range(MAX_PENDING_STEER_MESSAGES + 1):
            pipe.send_text(f"queued {index}\x11")
        time.sleep(0.2)
        pipe.send_text("\x03")
        assert cancelled.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("\x03")

    _result, transcript = _run_live_tui(session_builder=BlockingSession, feed=feed)

    assert calls == ["work"]
    assert any("Queue is full (16)" in text for role, text in transcript if role == "warn")
    assert any(
        "Discarded 16 pending messages" in text for role, text in transcript if role == "warn"
    )


def test_undelivered_steering_is_rescued_as_follow_up() -> None:
    first_started = threading.Event()
    third_started = threading.Event()
    calls: list[str] = []

    class LateSteerSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = cancellation_token
            calls.append(text)
            if len(calls) == 1:
                first_started.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    inbox = steer_inbox_for(self)
                    if inbox is not None and inbox.pending_count():
                        return 0
                    time.sleep(0.005)
                raise AssertionError("late steering was not queued")
            if len(calls) == 3:
                third_started.set()
            return 0

    def feed(pipe: Any) -> None:
        pipe.send_text("first\r")
        assert first_started.wait(timeout=2)
        pipe.send_text("queued before late steer\x11")
        pipe.send_text("late correction\r")
        assert third_started.wait(timeout=2)
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=LateSteerSession, feed=feed)

    assert result == "/exit"
    assert calls == ["first", "queued before late steer", "late correction"]
    assert any("Running queued message" in text for _role, text in transcript)


def test_interrupt_discards_steering_and_queued_follow_up() -> None:
    started = threading.Event()
    cancelled = threading.Event()
    calls: list[str] = []

    class InterruptSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            calls.append(text)
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    cancelled.set()
                    return 0
                time.sleep(0.005)
            raise AssertionError("turn was not cancelled")

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("steer me\r")
        time.sleep(0.05)
        pipe.send_text("queue me\x11")
        time.sleep(0.05)
        pipe.send_text("/mode auto\r")
        time.sleep(0.05)
        pipe.send_text("\x03")
        assert cancelled.wait(timeout=2)
        time.sleep(0.05)
        pipe.send_text("/exit\r")

    result, transcript = _run_live_tui(session_builder=InterruptSession, feed=feed)

    assert result == "/exit"
    assert calls == ["work"]
    assert any("Discarded 3 pending items" in text for role, text in transcript if role == "warn")


def test_tab_blocks_persona_cycle_mid_turn_but_shift_tab_stays_live() -> None:
    started = threading.Event()
    release = threading.Event()
    persona_calls: list[bool] = []
    state_holder: dict[str, Any] = {}

    class BlockingSession:
        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, text: str, *, cancellation_token: Any = None) -> int:
            _ = text, cancellation_token
            started.set()
            assert release.wait(timeout=3)
            return 0

    def persona_cycle() -> list[tuple[str, str]]:
        persona_calls.append(True)
        return []

    def feed(pipe: Any) -> None:
        pipe.send_text("work\r")
        assert started.wait(timeout=2)
        pipe.send_text("\t")
        time.sleep(0.05)
        pipe.send_text("\x1b[Z")
        time.sleep(0.05)
        release.set()
        time.sleep(0.1)
        pipe.send_text("/exit\r")

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    state = TuiState(model_name="test-model", username="tester", auto_approve=True)
    state_holder["state"] = state
    with create_pipe_input() as pipe:
        feeder = threading.Thread(target=lambda: feed(pipe), daemon=True)
        feeder.start()
        result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=BlockingSession,
            persona_cycle=persona_cycle,
            background_turns=True,
        )
        feeder.join(timeout=2)

    assert result == "/exit"
    assert not feeder.is_alive()
    assert persona_calls == []
    assert state_holder["state"].auto_approve is False
    assert any("/persona is unavailable" in text for role, text in transcript if role == "warn")
