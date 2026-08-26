from __future__ import annotations

import os
from pathlib import Path

from alysis_code.agent.tools_assembly import ToolDef, build_tools
from alysis_code.config import AppConfig, set_config_value
from alysis_code.session_store import SessionStore


def _tools(
    tmp_path: Path,
    *,
    session_id: str,
    read_ledger_enabled: bool = True,
) -> tuple[dict[str, ToolDef], SessionStore]:
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "sessions",
        session_id=session_id,
        cwd=os.fspath(tmp_path),
        repo_root=os.fspath(tmp_path),
    )
    tools = build_tools(
        root=tmp_path,
        console=None,
        store=store,
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model", read_ledger_enabled=read_ledger_enabled),
        subagents_enabled=False,
    )
    return tools, store


def _write_lines(path: Path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def test_exact_unchanged_reread_is_replaced_by_compact_notice(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two", "three", "four")
    tools, store = _tools(tmp_path, session_id="exact")
    try:
        first = tools["fs_read"].run({"path": "demo.txt"})
        second = tools["fs_read"].run({"path": "demo.txt"})

        assert first["content"] == "one\ntwo\nthree\nfour\n"
        assert second["read_ledger_skipped"] is True
        assert "lines 1-4 of demo.txt were already returned" in second["content"]
        assert "one\ntwo" not in second["content"]
    finally:
        store.close()


def test_partial_overlap_returns_only_unread_delta_and_notice(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two", "three", "four", "five", "six")
    tools, store = _tools(tmp_path, session_id="partial")
    try:
        first = tools["fs_read_lines"].run({"path": "demo.txt", "start_line": 2, "end_line": 4})
        second = tools["fs_read_lines"].run({"path": "demo.txt", "start_line": 3, "end_line": 6})

        assert "2: two" in first["content"]
        assert second["read_ledger_partial"] is True
        assert second["returned_ranges"] == [{"start_line": 5, "end_line": 6}]
        assert second["skipped_ranges"] == [{"start_line": 3, "end_line": 4}]
        assert "5: five" in second["content"]
        assert "6: six" in second["content"]
        assert "3: three" not in second["content"]
        assert "4: four" not in second["content"]
        assert "lines 3-4 of demo.txt were already returned" in second["content"]
    finally:
        store.close()


def test_clipped_line_is_not_recorded_as_fully_returned(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "abcdefghijklmnopqrstuvwxyz")
    tools, store = _tools(tmp_path, session_id="clipped-line")
    try:
        clipped = tools["fs_read_lines"].run(
            {
                "path": "demo.txt",
                "start_line": 1,
                "end_line": 1,
                "max_bytes": 10,
            }
        )
        complete = tools["fs_read_lines"].run(
            {
                "path": "demo.txt",
                "start_line": 1,
                "end_line": 1,
                "max_bytes": 1_000,
            }
        )

        assert clipped["line_clipped"] is True
        assert "read_ledger_skipped" not in complete
        assert "1: abcdefghijklmnopqrstuvwxyz" in complete["content"]
    finally:
        store.close()


def test_truncated_full_read_records_only_complete_lines(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "abcdefghijklmnopqrstuvwxyz")
    tools, store = _tools(tmp_path, session_id="truncated-full-read")
    try:
        truncated = tools["fs_read"].run({"path": "demo.txt", "max_bytes": 10})
        complete = tools["fs_read"].run({"path": "demo.txt", "max_bytes": 1_000})

        assert truncated["line_clipped"] is True
        assert complete["read_ledger_partial"] is True
        assert complete["returned_ranges"] == [{"start_line": 2, "end_line": 2}]
        assert "abcdefghijklmnopqrstuvwxyz" in complete["content"]
        assert "one\n" not in complete["content"]
    finally:
        store.close()


def test_observed_file_modification_invalidates_prior_ranges(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    _write_lines(target, "old-one", "old-two")
    tools, store = _tools(tmp_path, session_id="external-change")
    try:
        tools["fs_read_lines"].run({"path": "demo.txt", "start_line": 1, "end_line": 2})
        _write_lines(target, "new-one", "new-two")
        result = tools["fs_read_lines"].run({"path": "demo.txt", "start_line": 1, "end_line": 2})

        assert "new-one" in result["content"]
        assert "read_ledger_skipped" not in result
        assert "read_ledger_partial" not in result
    finally:
        store.close()


def test_session_write_invalidates_touched_file(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "before")
    tools, store = _tools(tmp_path, session_id="session-write")
    try:
        tools["fs_read"].run({"path": "demo.txt"})
        tools["fs_write"].run({"path": "demo.txt", "content": "after\n"})
        result = tools["fs_read"].run({"path": "demo.txt"})

        assert result["content"] == "after\n"
        assert "read_ledger_skipped" not in result
    finally:
        store.close()


def test_force_bypasses_unchanged_range_dedupe(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two")
    tools, store = _tools(tmp_path, session_id="force")
    try:
        tools["fs_read_lines"].run({"path": "demo.txt", "start_line": 1, "end_line": 2})
        result = tools["fs_read_lines"].run(
            {"path": "demo.txt", "start_line": 1, "end_line": 2, "force": True}
        )

        assert result["read_ledger_forced"] is True
        assert "1: one" in result["content"]
        assert "2: two" in result["content"]
    finally:
        store.close()


def test_parent_and_child_sessions_have_independent_ledgers(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two")
    parent_tools, parent_store = _tools(tmp_path, session_id="parent")
    child_tools, child_store = _tools(tmp_path, session_id="child")
    try:
        parent_tools["fs_read"].run({"path": "demo.txt"})
        child_result = child_tools["fs_read"].run({"path": "demo.txt"})

        assert child_result["content"] == "one\ntwo\n"
        assert "read_ledger_skipped" not in child_result
    finally:
        child_store.close()
        parent_store.close()


def test_disabled_read_ledger_restores_repeated_content(tmp_path: Path) -> None:
    _write_lines(tmp_path / "demo.txt", "one", "two")
    tools, store = _tools(tmp_path, session_id="disabled", read_ledger_enabled=False)
    try:
        first = tools["fs_read"].run({"path": "demo.txt"})
        second = tools["fs_read"].run({"path": "demo.txt"})

        assert first == second
        assert "read_ledger_skipped" not in second
    finally:
        store.close()


def test_read_ledger_config_and_force_schemas() -> None:
    cfg = AppConfig()
    assert cfg.read_ledger_enabled is True
    assert set_config_value(cfg, "read_ledger_enabled", "false").read_ledger_enabled is False

    from alysis_code.tools.registry import require_builtin_tool_metadata

    for name in ("fs_read", "fs_read_lines"):
        force = require_builtin_tool_metadata(name).parameters["properties"]["force"]
        assert force["type"] == "boolean"
        assert force["default"] is False
