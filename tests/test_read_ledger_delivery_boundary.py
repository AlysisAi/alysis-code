"""Actual tool delivery determines which file ranges may be deduplicated."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)

import alysis_code.agent_loop as agent_loop_mod
import alysis_code.compaction.tool_output_offload as offload_mod
from alysis_code.agent import session as session_mod
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall


def _read(call_id: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call_id, name="fs_read", arguments=arguments)


def _response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(content="", tool_calls=list(calls), raw={})


class _Client:
    model = "offline-ledger-delivery"
    temperature = 0.0

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses

    def chat(self, **_kwargs: Any) -> LLMResponse:
        assert self.responses, "Unexpected additional model request"
        return self.responses.pop(0)


def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: _Client, **kwargs: Any):
    monkeypatch.setattr(session_mod, "_make_session_llm_client", lambda **_kwargs: client)
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    cfg = AppConfig(
        model=client.model,
        routing_mode="code_only",
        skills_enabled=False,
        stream=False,
    )
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {client.model: {"context_window_tokens": 128_000, "max_output_tokens": 4096}},
        },
    }
    return session_mod.create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="unused-local-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
        verification_enabled=False,
        subagents_enabled=False,
        **kwargs,
    )


def _messages(session) -> dict[str, dict[str, Any]]:
    return {
        message["tool_call_id"]: json.loads(message["content"])
        for message in session.messages
        if message.get("role") == "tool"
    }


def _large_text() -> str:
    return "".join(f"line-{i:03d}: " + "evidence " * 7 + "\n" for i in range(1, 121))


@pytest.mark.parametrize("numbered", [False, True], ids=["raw", "numbered"])
@pytest.mark.parametrize("schedule", ["serial", "duplicate", "same-batch-window"])
def test_offloaded_read_does_not_hide_unseen_source_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, numbered: bool, schedule: str
) -> None:
    large = {"path": "report.txt"}
    if numbered:
        large.update(start_line=1, end_line=120)
    narrow = {
        "path": "report.txt",
        "start_line": 100,
        "end_line": 102,
        "include_line_numbers": numbered,
    }
    first = [_read("large", large)]
    if schedule == "duplicate":
        first.append(_read("duplicate", large))
    if schedule == "same-batch-window":
        first.append(_read("unseen", narrow))
    responses = [_response(*first)]
    if schedule != "same-batch-window":
        responses.append(_response(_read("unseen", narrow)))
    responses.extend(
        [
            _response(_read("repeat", narrow)),
            LLMResponse(content="Inspection complete.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, monkeypatch, _Client(responses))
    original = _large_text()
    (session.root / "report.txt").write_text(original)
    try:
        assert session.run_turn("Inspect report.txt and its lines 100 through 102.") == 0
        results = _messages(session)
        assert results["large"]["offloaded"] is True
        assert "line-100:" not in results["large"]["preview"]
        assert "line-100:" in results["unseen"]["content"]
        assert "line-102:" in results["unseen"]["content"]
        assert not results["unseen"].get("read_ledger_skipped")
        # The newly delivered small window is reusable on the next model step.
        assert results["repeat"]["read_ledger_skipped"] is True
        locator = results["large"]["artifact_locator"]
        artifact = session.store.session_artifact_layout.resolve_locator(locator)
        saved = json.loads(artifact.read_text())
        full = json.loads(saved["content_json"])
        assert "line-100:" in full["content"]
        assert "line-120:" in full["content"]
        assert (session.root / "report.txt").read_text() == original
    finally:
        session.close()


def test_shaped_read_after_artifact_failure_does_not_claim_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated artifact storage failure")

    monkeypatch.setattr(offload_mod, "_atomic_private_write_text", fail_write)
    session = _session(
        tmp_path,
        monkeypatch,
        _Client(
            [
                _response(_read("large", {"path": "report.txt", "start_line": 1})),
                _response(
                    _read("unseen", {"path": "report.txt", "start_line": 100, "end_line": 102})
                ),
                LLMResponse(content="Inspection complete.", tool_calls=[], raw={}),
            ]
        ),
    )
    (session.root / "report.txt").write_text(_large_text())
    try:
        assert session.run_turn("Inspect report.txt and lines 100 through 102.") == 0
        results = _messages(session)
        assert results["large"]["offloaded"] is False
        assert results["large"]["transcript_shaped"] is True
        assert not results["large"].get("artifact_saved")
        assert "simulated artifact storage failure" in results["large"]["error"]
        assert "line-100:" not in results["large"]["preview"]
        assert "line-100:" in results["unseen"]["content"]
    finally:
        session.close()


def test_offloaded_active_workdir_path_and_workspace_alias_share_delivery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "workspace" / "資料"
    nested.mkdir(parents=True)
    (nested / "report.txt").write_text(_large_text())
    session = _session(
        tmp_path,
        monkeypatch,
        _Client(
            [
                _response(_read("large", {"path": "./report.txt", "start_line": 1})),
                _response(
                    _read(
                        "unseen",
                        {
                            "path": "資料/report.txt",
                            "path_base": "workspace_root",
                            "start_line": 100,
                            "end_line": 102,
                        },
                    )
                ),
                LLMResponse(content="Inspection complete.", tool_calls=[], raw={}),
            ]
        ),
        active_workdir_relpath_override="資料",
    )
    try:
        assert session.run_turn("Inspect the current report and its lines 100 through 102.") == 0
        results = _messages(session)
        assert results["large"]["offloaded"] is True
        assert results["unseen"]["path"] == "資料/report.txt"
        assert "line-100:" in results["unseen"]["content"]
    finally:
        session.close()


def test_delivered_small_reads_keep_batch_reuse_and_detect_external_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_count = 0
    real_read = agent_loop_mod.fs_read

    def counted_read(*args: Any, **kwargs: Any):
        nonlocal read_count
        read_count += 1
        return real_read(*args, **kwargs)

    monkeypatch.setattr(agent_loop_mod, "fs_read", counted_read)
    args = {"path": "report.txt", "start_line": 1, "end_line": 2}
    client = _Client(
        [
            _response(_read("first", args), _read("cached", args)),
            _response(_read("unchanged", args)),
            LLMResponse(content="Initial inspection complete.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, monkeypatch, client)
    path = session.root / "report.txt"
    path.write_text("old first\nold second\n")
    try:
        assert session.run_turn("Inspect the current report.") == 0
        results = _messages(session)
        assert results["first"] == results["cached"]
        assert results["unchanged"]["read_ledger_skipped"] is True
        assert read_count == 2  # One first read, one ledger-checked subsequent step.
        # An external edit is visible without forcing or disabling the ledger.
        path.write_text("new first\nnew second\n")
        client.responses.extend(
            [
                _response(_read("changed", args)),
                LLMResponse(content="Reinspection complete.", tool_calls=[], raw={}),
            ]
        )
        assert session.run_turn("Inspect the updated report.") == 0
        assert "new second" in _messages(session)["changed"]["content"]
        assert not _messages(session)["changed"].get("read_ledger_skipped")
        assert read_count == 3
    finally:
        session.close()
