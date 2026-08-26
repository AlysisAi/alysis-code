from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall


class _ScriptedClient:
    model = "test-model"
    temperature = 0.0

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self._index = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = stream, on_text_delta, temperature
        self.calls.append({"messages": list(messages), "tools": tools})
        response = self._responses[self._index]
        self._index += 1
        return response


def _cfg() -> AppConfig:
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False, max_steps=4)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "test-model": {"context_window_tokens": 4096, "max_output_tokens": 512},
            },
            "default": {"context_window_tokens": 4096, "max_output_tokens": 512},
        }
    }
    return cfg


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _make_session(tmp_path: Path, client: _ScriptedClient):
    session = create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        enable_tool_output_offload=False,
        enable_conversation_summarization=False,
    )
    session.client = client  # type: ignore[assignment]
    return session


def _patch_read_counters(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    original_read = agent_loop_mod.fs_read
    original_read_lines = agent_loop_mod.fs_read_lines
    counts = {"fs_read": 0, "fs_read_lines": 0}

    def _wrapped_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        counts["fs_read"] += 1
        return original_read(*args, **kwargs)

    def _wrapped_read_lines(*args: Any, **kwargs: Any) -> dict[str, Any]:
        counts["fs_read_lines"] += 1
        return original_read_lines(*args, **kwargs)

    monkeypatch.setattr(agent_loop_mod, "fs_read", _wrapped_read)
    monkeypatch.setattr(agent_loop_mod, "fs_read_lines", _wrapped_read_lines)
    return counts


def _tool_messages(session) -> list[dict[str, Any]]:
    return [message for message in session.messages if str(message.get("role")) == "tool"]


def test_same_batch_duplicate_fs_read_lines_is_short_circuited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the fixture byte-exact across hosts. ``Path.write_text`` performs
    # newline translation on Windows, while this assertion intentionally
    # verifies that the cached result preserves the file's original bytes.
    (tmp_path / "demo.txt").write_bytes(b"one\ntwo\nthree\nfour\n")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call(
                        "tc-1",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 2, "end_line": 3},
                    ),
                    _tool_call(
                        "tc-2",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 2, "end_line": 3},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Inspect the same range twice.")
        tool_messages = _tool_messages(session)
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read_lines"] == 1
    assert len(tool_messages) == 2
    assert json.loads(str(tool_messages[0]["content"])) == json.loads(
        str(tool_messages[1]["content"])
    )


def test_same_batch_duplicate_fs_read_is_short_circuited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call("tc-1", "fs_read", {"path": "demo.txt", "max_bytes": 200}),
                    _tool_call("tc-2", "fs_read", {"path": "./demo.txt", "max_bytes": 200}),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Inspect the same file twice.")
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read"] == 1


def test_same_batch_read_lines_reuses_earlier_full_fs_read_when_untruncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The assertion below verifies byte-preserving reuse, so avoid Windows
    # text-mode newline translation in the fixture itself.
    (tmp_path / "demo.txt").write_bytes(b"one\ntwo\nthree\nfour\n")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call("tc-1", "fs_read", {"path": "demo.txt", "max_bytes": 500}),
                    _tool_call(
                        "tc-2",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 2, "end_line": 3},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Inspect the file and then a line range.")
        tool_messages = _tool_messages(session)
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read"] == 1
    assert counts["fs_read_lines"] == 0
    ranged_result = json.loads(str(tool_messages[-1]["content"]))
    assert ranged_result == {
        "path": "demo.txt",
        "start_line": 2,
        "end_line": 3,
        "total_lines": None,
        "content": "2: two\n3: three\n",
        "truncated": False,
    }


def test_same_batch_read_lines_reuses_broader_prior_read_lines_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_bytes(b"one\ntwo\nthree\nfour\nfive\n")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call(
                        "tc-1",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 5},
                    ),
                    _tool_call(
                        "tc-2",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 2, "end_line": 3},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Inspect the file and then a narrower range.")
        tool_messages = _tool_messages(session)
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read_lines"] == 1
    narrowed = json.loads(str(tool_messages[-1]["content"]))
    assert narrowed["content"] == "2: two\n3: three\n"
    assert narrowed["start_line"] == 2
    assert narrowed["end_line"] == 3


def test_same_batch_does_not_reuse_truncated_fs_read_for_read_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_text(("line\n" * 2000), encoding="utf-8")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call("tc-1", "fs_read", {"path": "demo.txt", "max_bytes": 12}),
                    _tool_call(
                        "tc-2",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 2},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Inspect and then read lines.")
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read"] == 1
    assert counts["fs_read_lines"] == 1


def test_same_batch_exact_duplicate_truncated_fs_read_is_short_circuited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_text(("line\n" * 2000), encoding="utf-8")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call("tc-1", "fs_read", {"path": "demo.txt", "max_bytes": 12}),
                    _tool_call("tc-2", "fs_read", {"path": "./demo.txt", "max_bytes": 12}),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Inspect the same truncated read twice.")
        tool_messages = _tool_messages(session)
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read"] == 1
    assert len(tool_messages) == 2
    first_result = json.loads(str(tool_messages[0]["content"]))
    second_result = json.loads(str(tool_messages[1]["content"]))
    assert first_result["path"] == "demo.txt"
    assert second_result["path"] == "./demo.txt"
    assert {k: v for k, v in first_result.items() if k != "path"} == {
        k: v for k, v in second_result.items() if k != "path"
    }


def test_same_batch_does_not_reuse_when_arguments_differ_meaningfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call("tc-1", "fs_read", {"path": "demo.txt", "max_bytes": 20}),
                    _tool_call("tc-2", "fs_read", {"path": "demo.txt", "max_bytes": 30}),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Inspect the file with different bounds.")
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read"] == 2


def test_same_batch_cache_is_invalidated_after_mutating_tool_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call(
                        "tc-1",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 2},
                    ),
                    _tool_call("tc-2", "fs_mkdir", {"path": "scratch"}),
                    _tool_call(
                        "tc-3",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 2},
                    ),
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Read, mutate, then read again.")
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read_lines"] == 2


def test_same_batch_reuse_does_not_cross_step_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call(
                        "tc-1",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 2},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call(
                        "tc-2",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 2},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        exit_code = session.run_turn("Read the same lines across two steps.")
    finally:
        session.close()

    assert exit_code == 0
    assert counts["fs_read_lines"] == 2


def test_same_batch_reuse_does_not_cross_turn_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "demo.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    counts = _patch_read_counters(monkeypatch)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call(
                        "tc-1",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 2},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call(
                        "tc-2",
                        "fs_read_lines",
                        {"path": "demo.txt", "start_line": 1, "end_line": 2},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Done again.", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(tmp_path, client)

    try:
        first_exit = session.run_turn("Read the same lines once.")
        second_exit = session.run_turn("Read the same lines again.")
    finally:
        session.close()

    assert first_exit == 0
    assert second_exit == 0
    assert counts["fs_read_lines"] == 2
