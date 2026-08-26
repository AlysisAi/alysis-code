from __future__ import annotations

import pytest

from alysis_code.pipeline_facts import (
    PIPELINE_STATUS_SENTINEL,
    build_pipeline_status_capture_command,
    command_has_top_level_pipe,
    extract_pipeline_status,
    pipeline_meaningful_stage,
    resolve_pipeline_stage_status,
    split_top_level_pipeline,
)


@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest -q", False),
        ("pytest -x foo | tail -40", True),
        ("a | b | c", True),
        ("echo 'a | b'", False),  # pipe inside quotes is not top-level
        ("pytest -q || true", False),  # || is not a pipe
        ("make build && echo done", False),
        ("cd repo && pytest | tail -5", True),
    ],
)
def test_command_has_top_level_pipe(command: str, expected: bool) -> None:
    assert command_has_top_level_pipe(command) is expected


def test_split_top_level_pipeline_pure() -> None:
    assert split_top_level_pipeline("pytest -x foo | tail -40") == ["pytest -x foo", "tail -40"]
    assert split_top_level_pipeline("a | b | c") == ["a", "b", "c"]


def test_split_top_level_pipeline_rejects_impure_and_plain() -> None:
    assert split_top_level_pipeline("pytest -q") is None  # no pipe
    assert split_top_level_pipeline("cd repo && pytest | tail") is None  # mixed operators
    assert split_top_level_pipeline("a | b || c") is None


@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest -x foo | tail -40", "pytest -x foo"),
        ("a | b | c", "a"),
        ("cd repo && pytest | tail -5", "pytest"),  # trailing pipeline's first stage
        ("pytest -q", None),  # not a pipeline
        ("make build && echo done", None),  # trailing group is not a pipeline
        ("pytest -q || true", None),  # short-circuit excluded
        ("echo 'x | y'", None),  # quoted pipe is not a pipeline
    ],
)
def test_pipeline_meaningful_stage(command: str, expected: str | None) -> None:
    assert pipeline_meaningful_stage(command) == expected


def test_build_capture_command_and_extract_roundtrip() -> None:
    wrapper = build_pipeline_status_capture_command("pytest -x foo | tail -40")
    # The wrapper falls back to plain sh when bash is unavailable, and never
    # enables pipefail.
    assert "command -v bash" in wrapper
    assert "pipefail" not in wrapper
    assert "PIPESTATUS" in wrapper

    # Simulate the wrapper's stderr: the command's own stderr, then the sentinel.
    real_stderr = "collected 3 items\n1 failed, 2 passed\n"
    captured_stderr = real_stderr + PIPELINE_STATUS_SENTINEL + "1 0\n"
    status, cleaned = extract_pipeline_status(captured_stderr)
    assert status == [1, 0]
    # Byte-exact recovery of the original stderr.
    assert cleaned == real_stderr


def test_extract_without_sentinel_returns_none_and_original() -> None:
    status, cleaned = extract_pipeline_status("just some stderr\n")
    assert status is None
    assert cleaned == "just some stderr\n"


def test_extract_preserves_stderr_without_trailing_newline() -> None:
    real_stderr = "no trailing newline"
    captured = real_stderr + PIPELINE_STATUS_SENTINEL + "0\n"
    status, cleaned = extract_pipeline_status(captured)
    assert status == [0]
    assert cleaned == real_stderr


def test_resolve_stage_status_passthrough_when_known() -> None:
    calls: list[str] = []

    def reexec(stage: str) -> int | None:
        calls.append(stage)
        return 0

    assert resolve_pipeline_stage_status("pytest | tail", [1, 0], reexec=reexec) == [1, 0]
    assert calls == []  # never re-runs when status is already known


def test_resolve_stage_status_reexecutes_exactly_once() -> None:
    calls: list[str] = []

    def reexec(stage: str) -> int | None:
        calls.append(stage)
        return 1

    resolved = resolve_pipeline_stage_status("pytest -x foo | tail", None, reexec=reexec)
    assert resolved == [1]
    assert calls == ["pytest -x foo"]  # first stage, exactly once


def test_resolve_stage_status_none_without_reexec() -> None:
    assert resolve_pipeline_stage_status("pytest | tail", None, reexec=None) is None


def test_resolve_stage_status_none_when_reexec_fails() -> None:
    assert resolve_pipeline_stage_status("pytest | tail", None, reexec=lambda _stage: None) is None
