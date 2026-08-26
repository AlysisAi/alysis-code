"""Verification-command selection is an optimization, never a precondition.

Selecting an authoritative command used to be able to abort a run before the
agent had done any work: an inferred command Alysis Code could not classify (most
often ``python -m doctest README.rst`` on an old-style repo) raised and the whole
session exited non-zero having produced nothing. These tests pin the replacement
behaviour -- degrade, warn, and keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.config import AppConfig
from alysis_code.repo_scan import (
    RepoScanResult,
    detect_fallback_test_commands,
    scan_workspace,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import read_session_events
from alysis_code.verify_gate import (
    ResolvedVerifyCommands,
    VerifyError,
    command_is_docs_doctest,
    repair_invalid_verify_command_selection,
    resolve_verify_command_selection,
)
from alysis_code.workspace_context import resolve_workspace_context

_UNKNOWN_COMMAND = "mystery-runner --all"
_DOCS_DOCTEST_COMMAND = "python -m doctest README.rst"


def _scan(root: Path, commands: list[str]) -> RepoScanResult:
    real = scan_workspace(context=resolve_workspace_context(root))
    payload = real.to_dict()
    payload["likely_test_commands"] = list(commands)
    return RepoScanResult.from_dict(payload)


def _inferred_selection(commands: tuple[str, ...]) -> ResolvedVerifyCommands:
    return ResolvedVerifyCommands(
        commands=commands,
        source="repo_scan.likely_test_commands",
        reason="repo scan discovered authoritative repo-native verification commands",
        contract_type="repo_native",
    )


def _prepare(
    root: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    likely_test_commands: list[str],
) -> Any:
    monkeypatch.setattr(
        agent_loop_mod,
        "scan_workspace",
        lambda *, context: _scan(root, likely_test_commands),
        raising=False,
    )
    return agent_loop_mod.prepare_session_prompt_context(
        cfg=AppConfig(model="test-model", subagents_enabled=False, skills_enabled=False),
        root=root,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
    )


# ---------------------------------------------------------------------------
# (c) doctest over documentation is never an authoritative verification surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -m doctest README.rst",
        "python3 -m doctest README.md",
        "/opt/alysis-venv/bin/python -m doctest README.rst",
        "python -m doctest docs/usage.rst docs/install.rst",
        "PYTHONPATH=. python -m doctest README.md",
        "uv run python -m doctest README.md",
        "python -m pytest --doctest-glob=README.md -q README.md",
        "pytest --doctest-glob=CHANGELOG.md -q CHANGELOG.md",
    ],
)
def test_docs_doctest_commands_are_recognized(command: str) -> None:
    assert command_is_docs_doctest(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "python -m pytest -q tests",
        "python -m unittest discover",
        "python -m doctest src/mathlet.py",
        # A doctest run that also covers code is a real verification surface.
        "python -m doctest mathlet/core.py README.md",
        # The glob only makes it docs-doctest when the targets are docs too.
        "pytest --doctest-glob=README.md -q tests",
        "python -m pytest --doctest-modules -q src",
        "make test",
    ],
)
def test_real_verification_commands_are_not_treated_as_docs_doctest(command: str) -> None:
    assert command_is_docs_doctest(command) is False


def test_reported_readme_doctest_startup_failure_no_longer_aborts_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact reported regression: startup died before doing any work."""
    (tmp_path / "README.rst").write_text(">>> 1 + 1\n2\n", encoding="utf-8")
    (tmp_path / "test_thing.py").write_text("def test_ok() -> None:\n    pass\n", "utf-8")

    prompt_context = _prepare(
        tmp_path,
        monkeypatch=monkeypatch,
        likely_test_commands=[
            "pytest -q",
            "/opt/alysis-venv/bin/python -m doctest README.rst",
        ],
    )

    assert prompt_context.effective_verification_commands == ["pytest -q"]


def test_repo_scan_docs_doctest_commands_are_never_selected(tmp_path: Path) -> None:
    (tmp_path / "README.rst").write_text(">>> 1 + 1\n2\n", encoding="utf-8")
    cfg = AppConfig(model="test-model")

    selection = resolve_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=tmp_path,
        repo_scan=_scan(tmp_path, [_DOCS_DOCTEST_COMMAND]),
    )

    assert _DOCS_DOCTEST_COMMAND not in selection.commands
    assert selection.source != "repo_scan.likely_test_commands"


# ---------------------------------------------------------------------------
# (a) an invalid selected command degrades instead of aborting the run
# ---------------------------------------------------------------------------


def test_invalid_inferred_command_falls_through_to_a_detected_runner(tmp_path: Path) -> None:
    (tmp_path / "test_mathlet.py").write_text("def test_ok() -> None:\n    pass\n", "utf-8")

    repair = repair_invalid_verify_command_selection(
        _inferred_selection((_UNKNOWN_COMMAND,)),
        root=tmp_path,
        repo_scan=_scan(tmp_path, [_UNKNOWN_COMMAND]),
    )

    assert repair.dropped_commands == (_UNKNOWN_COMMAND,)
    assert repair.selection.commands == ("pytest -q",)
    assert repair.selection.best_effort is False
    assert _UNKNOWN_COMMAND in repair.warning


def test_invalid_command_alongside_a_valid_one_keeps_only_the_valid_one(tmp_path: Path) -> None:
    repair = repair_invalid_verify_command_selection(
        _inferred_selection(("pytest -q", _UNKNOWN_COMMAND)),
        root=tmp_path,
        repo_scan=_scan(tmp_path, ["pytest -q", _UNKNOWN_COMMAND]),
    )

    assert repair.selection.commands == ("pytest -q",)
    assert repair.selection.source == "repo_scan.likely_test_commands"
    assert repair.selection.best_effort is False
    assert repair.dropped_commands == (_UNKNOWN_COMMAND,)


def test_explicitly_requested_command_is_kept_with_a_warning(tmp_path: Path) -> None:
    selection = ResolvedVerifyCommands(
        commands=(_UNKNOWN_COMMAND,),
        source="cli.verify_cmd",
        reason="explicit verification override supplied by the user",
        contract_type="explicit_override",
    )

    repair = repair_invalid_verify_command_selection(selection, root=tmp_path)

    assert repair.selection.commands == (_UNKNOWN_COMMAND,)
    assert repair.dropped_commands == ()
    assert repair.warning


@pytest.mark.parametrize(
    "command, reason",
    [
        ("true", "vacuous_verifier"),
        ("cat build.log", "non_assertive_observation"),
        ("pytest -q || true", "disallowed_shell_control_flow"),
        ("printf ok | cat", "unsafe_pipeline"),
    ],
)
def test_a_refused_command_still_fails_fast(tmp_path: Path, command: str, reason: str) -> None:
    """Degrading is for commands Alysis Code cannot classify, not ones it refuses.

    A vacuous or failure-masking verifier would report a pass it did not earn,
    so quietly accepting it as the session's contract is worse than stopping.
    """
    selection = ResolvedVerifyCommands(
        commands=(command,),
        source="cli.verify_cmd",
        reason="explicit verification override supplied by the user",
        contract_type="explicit_override",
    )

    with pytest.raises(VerifyError, match=reason):
        repair_invalid_verify_command_selection(selection, root=tmp_path)


def test_a_refused_command_fails_fast_even_when_inferred(tmp_path: Path) -> None:
    with pytest.raises(VerifyError, match="vacuous_verifier"):
        repair_invalid_verify_command_selection(
            _inferred_selection(("true",)),
            root=tmp_path,
        )


def test_valid_selection_is_returned_untouched(tmp_path: Path) -> None:
    selection = _inferred_selection(("pytest -q",))

    repair = repair_invalid_verify_command_selection(selection, root=tmp_path)

    assert repair.selection is selection
    assert repair.dropped_commands == ()
    assert repair.warning == ""


def test_session_startup_continues_when_the_selected_command_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test_mathlet.py").write_text("def test_ok() -> None:\n    pass\n", "utf-8")

    prompt_context = _prepare(
        tmp_path,
        monkeypatch=monkeypatch,
        likely_test_commands=[_UNKNOWN_COMMAND],
    )

    assert prompt_context.effective_verification_commands == ["pytest -q"]
    assert prompt_context.verification_selection_warnings
    assert _UNKNOWN_COMMAND in prompt_context.verification_selection_warnings[0]
    assert prompt_context.effective_verification_selection.best_effort is False


# ---------------------------------------------------------------------------
# (b) a workspace with nothing to detect continues in best-effort mode
# ---------------------------------------------------------------------------


def test_workspace_without_any_test_surface_degrades_to_best_effort(tmp_path: Path) -> None:
    (tmp_path / "mathlet.py").write_text("VALUE = 1\n", encoding="utf-8")

    repair = repair_invalid_verify_command_selection(
        _inferred_selection((_UNKNOWN_COMMAND,)),
        root=tmp_path,
        repo_scan=_scan(tmp_path, [_UNKNOWN_COMMAND]),
    )

    assert repair.selection.commands == ()
    assert repair.selection.best_effort is True
    assert repair.selection.contract_type == "unavailable"
    assert "best-effort" in repair.warning


def test_session_startup_records_best_effort_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "mathlet.py").write_text("VALUE = 1\n", encoding="utf-8")

    prompt_context = _prepare(
        tmp_path,
        monkeypatch=monkeypatch,
        likely_test_commands=[_UNKNOWN_COMMAND],
    )

    selection = prompt_context.effective_verification_selection
    assert prompt_context.effective_verification_commands == []
    assert selection.best_effort is True
    assert prompt_context.verification_selection_warnings


def test_session_record_marks_verification_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mathlet.py").write_text("VALUE = 1\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(
        agent_loop_mod,
        "scan_workspace",
        lambda *, context: _scan(repo, [_UNKNOWN_COMMAND]),
        raising=False,
    )

    session = agent_loop_mod.create_session(
        cfg=AppConfig(model="test-model", subagents_enabled=False, skills_enabled=False),
        root=repo,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=False,
        api_key_override="override-key",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        session_log_dir_override=sessions_dir,
        session_id_override="best-effort-test",
    )

    assert session.verification_best_effort is True

    events = list(read_session_events(sessions_dir / "best-effort-test.jsonl"))
    starts = [event for event in events if event.get("type") == "session_start"]
    assert starts and starts[-1]["payload"]["verification_best_effort"] is True

    degraded = [
        event
        for event in events
        if event.get("type") == "warning"
        and event.get("payload", {}).get("warning") == "verification_selection_degraded"
    ]
    assert degraded, "a degraded verification contract must be warned about in the session record"
    assert _UNKNOWN_COMMAND in degraded[-1]["payload"]["message"]


def test_declared_pytest_config_is_detected_as_a_fallback_runner(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\ntestpaths = suite\n", encoding="utf-8")

    assert detect_fallback_test_commands(root=tmp_path) == ["pytest -q"]


@pytest.mark.parametrize(
    "config_name, config_body",
    [
        ("pytest.ini", ""),
        ("pyproject.toml", "[tool.pytest.ini_options]\n"),
        ("tox.ini", "[pytest]\n"),
        ("setup.cfg", "[tool:pytest]\n"),
    ],
)
def test_every_pytest_config_shape_is_a_fallback_signal(
    tmp_path: Path,
    config_name: str,
    config_body: str,
) -> None:
    (tmp_path / config_name).write_text(config_body, encoding="utf-8")

    assert detect_fallback_test_commands(root=tmp_path) == ["pytest -q"]


def test_conftest_is_a_fallback_signal(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")

    assert detect_fallback_test_commands(root=tmp_path) == ["pytest -q"]


def test_unittest_discoverable_modules_are_a_fallback_signal(tmp_path: Path) -> None:
    # `tests.py` matches unittest's default `test*.py` pattern but none of the
    # pytest naming conventions, so it is invisible to the primary inference.
    (tmp_path / "tests.py").write_text("import unittest\n", encoding="utf-8")

    assert detect_fallback_test_commands(root=tmp_path) == ["python -m unittest discover"]


def test_workspace_with_no_runner_detects_nothing(tmp_path: Path) -> None:
    (tmp_path / "mathlet.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert detect_fallback_test_commands(root=tmp_path) == []


# ---------------------------------------------------------------------------
# (d) an ordinary pytest repo selects exactly what it always did
# ---------------------------------------------------------------------------


def test_normal_pytest_repo_selection_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mathlet'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mathlet.py").write_text("def test_ok():\n    pass\n", "utf-8")

    selection = resolve_verify_command_selection(
        cfg=AppConfig(model="test-model"),
        verify_cmd=None,
        root=tmp_path,
    )

    assert selection.commands == ("pytest -q",)
    assert selection.source == "repo_scan.likely_test_commands"
    assert selection.contract_type == "repo_native"
    assert selection.best_effort is False


def test_normal_pytest_repo_startup_selects_the_repo_native_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mathlet.py").write_text("def test_ok():\n    pass\n", "utf-8")

    prompt_context = agent_loop_mod.prepare_session_prompt_context(
        cfg=AppConfig(model="test-model", subagents_enabled=False, skills_enabled=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
    )

    assert prompt_context.effective_verification_commands == ["pytest -q"]
    assert prompt_context.verification_selection_warnings == ()
    assert prompt_context.effective_verification_selection.best_effort is False


def test_explicit_verify_cmd_selection_is_unchanged(tmp_path: Path) -> None:
    selection = resolve_verify_command_selection(
        cfg=AppConfig(model="test-model"),
        verify_cmd=["pytest -q tests/test_mathlet.py"],
        root=tmp_path,
    )

    assert selection.commands == ("pytest -q tests/test_mathlet.py",)
    assert selection.source == "cli.verify_cmd"
    assert selection.best_effort is False
