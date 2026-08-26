"""Forge worker failure-capture: the full traceback is persisted, secret-redacted.

The worker result keeps only a short summary; for autonomous root-causing the full
agent traceback must survive to the per-task artifact dir, with secrets redacted, and
persistence must never mask the original agent error.
"""

from __future__ import annotations

from pathlib import Path

from alysis_code.swarm_worker import _persist_agent_exception_traceback


def test_persist_agent_exception_traceback_redacts_and_writes(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "scratch" / "T01"
    try:
        raise RuntimeError("boom with secret sk-ABCDEFGHIJKLMNOP1234")
    except RuntimeError as exc:
        _persist_agent_exception_traceback(artifact_dir, exc)

    out = artifact_dir / "agent_exception_traceback.txt"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "Traceback" in text
    assert "RuntimeError" in text
    assert "sk-ABCDEFGHIJKLMNOP1234" not in text


def test_persist_agent_exception_traceback_is_best_effort(tmp_path: Path) -> None:
    # Target sits under a regular file, so mkdir fails; the helper must swallow it.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "sub"
    try:
        raise ValueError("x")
    except ValueError as exc:
        _persist_agent_exception_traceback(target, exc)  # must not raise

    assert blocker.is_file()
