"""Unified turn path: no pre-turn routing, mode-derived posture.

The legacy pre-turn semantic router is deleted. No router client is ever
provisioned, every text turn goes straight to the main model with the full
per-mode agent surface, and execution posture derives from the execution
mode. ``unified_turn_path_enabled`` (and ``ALYSIS_UNIFIED_TURN_PATH``)
remain accepted for one release but are ignored: flag-off behaves identically
to flag-on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.agent.prompt_context import (
    _TASK_BRIEF_MARKER,
    refresh_session_task_brief_from_observed_turn,
)
from alysis_code.agent.reproduction_first import (
    REPRODUCTION_FIRST_CONDITIONAL_DIRECTIVE,
    ReproPhase,
    ReproRun,
)
from alysis_code.agent.turn_path import unified_turn_path_enabled
from alysis_code.agent.verification import TurnExecutionState
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.plan_mode import instruction_with_approved_plan
from alysis_code.session_store import read_session_events


class _FinalReplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.message_snapshots: list[list[dict[str, Any]]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = tools, stream, on_text_delta, temperature
        self.calls += 1
        self.message_snapshots.append(list(messages))
        return LLMResponse(content=self.reply, tool_calls=[], raw={})


class _SingleToolThenDoneClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0
        self.tool_enabled_calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        self.calls += 1
        if tools is None or self.tool_enabled_calls >= 1:
            return LLMResponse(content="Inspection complete.", tool_calls=[], raw={})
        self.tool_enabled_calls += 1
        return LLMResponse(
            content="Listing the workspace first.",
            tool_calls=[
                ToolCall(
                    id=f"call-{self.tool_enabled_calls}",
                    name="fs_list",
                    arguments={"path": "."},
                )
            ],
            raw={},
        )


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _unified_session(tmp_path: Path, *, mode: str = "review") -> Any:
    cfg = AppConfig(model="test-model", unified_turn_path_enabled=True)
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode=mode,
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )


# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------


def test_unified_turn_path_default_is_on(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_UNIFIED_TURN_PATH", raising=False)
    assert unified_turn_path_enabled(AppConfig(model="test-model")) is True
    assert unified_turn_path_enabled(None) is True


def test_unified_turn_path_config_value_still_parses_off(monkeypatch) -> None:
    # The resolver keeps honoring the stored value for one release even though
    # nothing consults it any more.
    monkeypatch.delenv("ALYSIS_UNIFIED_TURN_PATH", raising=False)
    cfg = AppConfig(model="test-model", unified_turn_path_enabled=False)
    assert unified_turn_path_enabled(cfg) is False


@pytest.mark.parametrize(
    ("env_value", "config_value", "expected"),
    [
        ("off", True, False),
        ("0", True, False),
        ("disabled", True, False),
        ("on", False, True),
        ("1", False, True),
        ("enabled", False, True),
        ("garbage", True, True),
        ("garbage", False, False),
    ],
)
def test_unified_turn_path_env_overrides_config(
    monkeypatch, env_value: str, config_value: bool, expected: bool
) -> None:
    monkeypatch.setenv("ALYSIS_UNIFIED_TURN_PATH", env_value)
    cfg = AppConfig(model="test-model", unified_turn_path_enabled=config_value)
    assert unified_turn_path_enabled(cfg) is expected


def test_set_config_value_roundtrips_unified_turn_path() -> None:
    cfg = AppConfig(model="test-model")
    cfg = set_config_value(cfg, "unified_turn_path_enabled", "true")
    assert cfg.unified_turn_path_enabled is True
    cfg = set_config_value(cfg, "unified_turn_path_enabled", "off")
    assert cfg.unified_turn_path_enabled is False
    with pytest.raises(ConfigError):
        set_config_value(cfg, "unified_turn_path_enabled", "sometimes")


# ---------------------------------------------------------------------------
# Session provisioning
# ---------------------------------------------------------------------------


def test_unified_session_provisions_no_router_client(tmp_path: Path) -> None:
    session = _unified_session(tmp_path)
    try:
        assert session.router_client is None
    finally:
        session.close()


def test_default_session_provisions_no_router_client(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_UNIFIED_TURN_PATH", raising=False)
    cfg = AppConfig(model="test-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert session.router_client is None
    finally:
        session.close()


def test_flag_off_session_provisions_no_router_client(tmp_path: Path, monkeypatch) -> None:
    # The flag is accepted-and-ignored: turning it off provisions exactly the
    # same router-free session as the default.
    monkeypatch.delenv("ALYSIS_UNIFIED_TURN_PATH", raising=False)
    cfg = AppConfig(model="test-model", unified_turn_path_enabled=False)
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=True,
        api_key_override="override-key",
    )
    try:
        assert session.router_client is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Turn behavior with the flag on
# ---------------------------------------------------------------------------


def test_unified_report_turn_runs_tools_without_router(tmp_path: Path, monkeypatch) -> None:
    session = _unified_session(tmp_path, mode="review")
    client = _SingleToolThenDoneClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Inspect this workspace and report what you find.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.tool_enabled_calls == 1
    assert _event_payloads(log_path, "route_decision") == []
    assert _event_payloads(log_path, "route_contract_unavailable") == []
    intents = _event_payloads(log_path, "turn_intent_resolved")
    assert len(intents) == 1
    assert intents[0]["repo_turn_execution_intent"] == "execute"
    assert intents[0]["execution_safeguards_enabled"] is True
    assert intents[0]["unified_turn_path"] is True
    assert _event_payloads(log_path, "interactive_no_material_edits_detected") == []
    operations = {
        str(payload.get("operation") or "") for payload in _event_payloads(log_path, "llm_usage")
    }
    assert "routing_llm" not in operations
    assert "routing_llm_contract_repair" not in operations


def test_unified_readonly_turn_posture_is_advisory(tmp_path: Path, monkeypatch) -> None:
    session = _unified_session(tmp_path, mode="readonly")
    client = _FinalReplyClient("Read-only summary of the workspace.")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("What does this workspace contain?")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    intents = _event_payloads(log_path, "turn_intent_resolved")
    assert len(intents) == 1
    assert intents[0]["repo_turn_execution_intent"] == "advisory_non_execution"
    assert intents[0]["execution_safeguards_enabled"] is False
    assert intents[0]["unified_turn_path"] is True


@pytest.mark.parametrize(
    "instruction",
    [
        "Inspect this workspace and report only; do not change any files.",
        (
            "\u0395\u03c0\u03b9\u03b8\u03b5\u03ce\u03c1\u03b7\u03c3\u03b5 \u03c4\u03bf\u03bd \u03c7\u03ce\u03c1\u03bf "
            "\u03b5\u03c1\u03b3\u03b1\u03c3\u03af\u03b1\u03c2 \u03ba\u03b1\u03b9 \u03b4\u03ce\u03c3\u03b5 \u03b1\u03bd\u03b1\u03c6\u03bf\u03c1\u03ac. "
            "\u039c\u03b7\u03bd \u03b1\u03bb\u03bb\u03ac\u03be\u03b5\u03b9\u03c2 \u03b1\u03c1\u03c7\u03b5\u03af\u03b1."
        ),
        "Inspecciona el repositorio y entrega un informe. No cambies ning\u00fan archivo.",
    ],
)
def test_report_only_pre_read_finishes_from_observed_readonly_behavior(
    tmp_path: Path,
    instruction: str,
) -> None:
    session = _unified_session(tmp_path, mode="review")
    client = _SingleToolThenDoneClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn(instruction)
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 2
    intents = _event_payloads(log_path, "turn_intent_resolved")
    assert len(intents) == 1
    assert intents[0]["classified_turn_intent"] == "execute"
    assert intents[0]["repo_turn_execution_intent"] == "execute"
    assert intents[0]["execution_safeguards_enabled"] is True
    assert _event_payloads(log_path, "interactive_no_material_edits_detected") == []


@pytest.mark.parametrize(
    ("instruction", "expected_intent"),
    [
        ("Fix the parser bug and add a regression test.", "execute"),
        ("Take a look at the parser.", "execute"),
    ],
)
def test_mutating_and_ambiguous_prompts_keep_execute_posture(
    tmp_path: Path,
    instruction: str,
    expected_intent: str,
) -> None:
    session = _unified_session(tmp_path, mode="review")
    client = _FinalReplyClient("Done.")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn(instruction)
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    intents = _event_payloads(log_path, "turn_intent_resolved")
    assert intents[0]["classified_turn_intent"] == expected_intent
    assert intents[0]["execution_safeguards_enabled"] is True
    assert REPRODUCTION_FIRST_CONDITIONAL_DIRECTIVE in "\n".join(
        str(message.get("content") or "") for message in client.message_snapshots[0]
    )


def test_unified_small_talk_flows_through_main_loop(tmp_path: Path, monkeypatch) -> None:
    session = _unified_session(tmp_path, mode="review")
    client = _FinalReplyClient("Hello! How can I help with your project?")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("hello")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    finals = _event_payloads(log_path, "final")
    assert len(finals) == 1
    assert finals[0]["content"] == "Hello! How can I help with your project?"
    assert _event_payloads(log_path, "route_decision") == []
    assert _event_payloads(log_path, "non_repo_router_reply_used") == []
    assert _event_payloads(log_path, "non_repo_router_reply_bypassed_for_streaming") == []


def test_unified_reply_language_config_injects_directive(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig(model="test-model", unified_turn_path_enabled=True, reply_language="Greek")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )
    client = _FinalReplyClient("Γεια σου! Πώς μπορώ να βοηθήσω;")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("hello")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    language_events = _event_payloads(log_path, "language_decision")
    assert len(language_events) == 1
    assert language_events[0]["language"] == "Greek"
    assert language_events[0]["language_source"] == "config"
    notes = _event_payloads(log_path, "system_note")
    directives = [n for n in notes if n.get("message") == "turn_language_script_directive"]
    assert len(directives) == 1
    assert directives[0]["language"] == "Greek"


def test_unified_without_reply_language_injects_no_directive(tmp_path: Path, monkeypatch) -> None:
    session = _unified_session(tmp_path)
    client = _FinalReplyClient("Hello!")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("hello")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    notes = _event_payloads(log_path, "system_note")
    assert [n for n in notes if n.get("message") == "turn_language_script_directive"] == []


# ---------------------------------------------------------------------------
# Observed-facts task brief (router-relation replacement)
# ---------------------------------------------------------------------------


def _brief_session() -> Any:
    return SimpleNamespace(messages=[], store=SimpleNamespace(workspace_kind="git_repo"))


def _brief_content(session: Any) -> str:
    for message in session.messages:
        content = str(message.get("content") or "")
        if content.lstrip().startswith(_TASK_BRIEF_MARKER):
            return content
    return ""


def test_observed_task_brief_updates_only_on_material_edits() -> None:
    session = _brief_session()

    refresh_session_task_brief_from_observed_turn(
        session, instruction="hello there", material_edit_count=0
    )
    assert "hello there" not in _brief_content(session)

    refresh_session_task_brief_from_observed_turn(
        session, instruction="Fix the flaky login test", material_edit_count=2
    )
    assert "Fix the flaky login test" in _brief_content(session)

    # Follow-up chatter without edits can never clobber the task statement.
    refresh_session_task_brief_from_observed_turn(
        session, instruction="thanks, looks good", material_edit_count=0
    )
    content = _brief_content(session)
    assert "thanks, looks good" not in content
    assert "Fix the flaky login test" in content

    # A new materially-productive instruction becomes current; the previous
    # current rotates into prior context instead of vanishing.
    refresh_session_task_brief_from_observed_turn(
        session, instruction="Now update the docs for the fix", material_edit_count=1
    )
    content = _brief_content(session)
    assert "Now update the docs for the fix" in content
    assert "Fix the flaky login test" in content

    # Slash commands are never task statements, whatever they touched.
    refresh_session_task_brief_from_observed_turn(
        session, instruction="/status", material_edit_count=3
    )
    assert "/status" not in _brief_content(session)


def test_observed_task_brief_accepts_approved_plan_at_turn_start() -> None:
    session = _brief_session()
    instruction = instruction_with_approved_plan(
        user_message="Add retry logic to the fetcher",
        approved_plan="1. Wrap fetch in retry\n2. Add tests",
    )

    refresh_session_task_brief_from_observed_turn(
        session, instruction=instruction, material_edit_count=0
    )
    assert "Add retry logic to the fetcher" in _brief_content(session)


# ---------------------------------------------------------------------------
# Reproduction-first: engagement-based gate (router task-shape replacement)
# ---------------------------------------------------------------------------


def _repro_run(*, phase: ReproPhase, passed: bool) -> ReproRun:
    return ReproRun(
        command="python repro_case.py",
        artifact_paths=("repro_case.py",),
        phase=phase,
        passed=passed,
    )


def test_repro_gate_binds_only_after_observed_failing_pre_fix_run() -> None:
    state = TurnExecutionState(execution_requested=True)

    # No prediction, no engagement: not applicable.
    assert not state.repro_protocol_applicable(
        enabled=True, turn_intent="execute", engagement_based=True
    )

    # A helper script that only ever passed never engages the gate.
    state.repro_runs.append(_repro_run(phase=ReproPhase.PRE_FIX, passed=True))
    assert not state.repro_protocol_applicable(
        enabled=True, turn_intent="execute", engagement_based=True
    )

    # A failing pre-fix run is the anchor: from here the protocol binds.
    state.repro_runs.append(_repro_run(phase=ReproPhase.PRE_FIX, passed=False))
    assert state.repro_protocol_applicable(
        enabled=True, turn_intent="execute", engagement_based=True
    )

    # Legacy behavior without the engagement flag is unchanged: no predicted
    # bug-fix shape means not applicable.
    assert not state.repro_protocol_applicable(enabled=True, turn_intent="execute")
    assert not state.repro_protocol_applicable(
        enabled=False, turn_intent="execute", engagement_based=True
    )
    assert not state.repro_protocol_applicable(
        enabled=True, turn_intent="advisory_non_execution", engagement_based=True
    )


class _CapturingReplyClient(_FinalReplyClient):
    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.seen_system_messages: list[str] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        for message in kwargs.get("messages") or []:
            if str(message.get("role") or "") == "system":
                self.seen_system_messages.append(str(message.get("content") or ""))
        return super().chat(**kwargs)


def test_unified_execute_turn_gets_conditional_repro_directive(tmp_path: Path, monkeypatch) -> None:
    session = _unified_session(tmp_path, mode="review")
    client = _CapturingReplyClient("Done.")
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Fix the crash in the parser.") == 0
    finally:
        session.close()

    assert any(
        REPRODUCTION_FIRST_CONDITIONAL_DIRECTIVE in content
        for content in client.seen_system_messages
    )


def test_unified_readonly_turn_gets_no_repro_directive(tmp_path: Path, monkeypatch) -> None:
    session = _unified_session(tmp_path, mode="readonly")
    client = _CapturingReplyClient("Summary.")
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("What does the parser module do?") == 0
    finally:
        session.close()

    assert not any(
        REPRODUCTION_FIRST_CONDITIONAL_DIRECTIVE in content
        for content in client.seen_system_messages
    )


class _WriteThenDoneClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0
        self.wrote = False

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        self.calls += 1
        if tools is None or self.wrote:
            return LLMResponse(content="Change applied.", tool_calls=[], raw={})
        self.wrote = True
        return LLMResponse(
            content="Writing the requested note.",
            tool_calls=[
                ToolCall(
                    id="w1",
                    name="fs_write",
                    arguments={"path": "notes.txt", "content": "hello\n"},
                )
            ],
            raw={},
        )


def test_unified_material_edit_turn_pins_instruction_into_task_brief(
    tmp_path: Path, monkeypatch
) -> None:
    # auto mode: fs_write needs no interactive approval, so the material edit
    # actually lands and the observed-facts rule has something to observe.
    session = _unified_session(tmp_path, mode="auto")
    client = _WriteThenDoneClient()
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Create notes.txt containing hello.") == 0
        brief = _brief_content(session)
    finally:
        session.close()

    assert client.wrote is True
    assert "Create notes.txt containing hello." in brief


def test_flag_off_behaves_identically_to_flag_on(tmp_path: Path, monkeypatch) -> None:
    # The legacy routed path is deleted: with the flag off, the turn still
    # takes the unified path (no routing call, one main-model call, and the
    # turn-intent payload records the unified path).
    monkeypatch.delenv("ALYSIS_UNIFIED_TURN_PATH", raising=False)
    cfg = AppConfig(model="test-model", routing_mode="auto", unified_turn_path_enabled=False)
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )
    client = _FinalReplyClient("Unified turn completed.")
    session.client = client  # type: ignore[assignment]

    try:
        assert session.router_client is None
        exit_code = session.run_turn("Summarize the workspace state.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 1
    assert _event_payloads(log_path, "route_decision") == []
    intents = _event_payloads(log_path, "turn_intent_resolved")
    assert len(intents) == 1
    assert intents[0]["unified_turn_path"] is True
