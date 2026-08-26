from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_root_conftest() -> ModuleType:
    module_path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("_alysis_root_test_conftest", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load root test conftest from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


forge_conftest = _load_root_conftest()


def _request_for_tmp_path(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        node=SimpleNamespace(
            nodeid="tests/test_forge_exec.py::test_hangs",
            name="test_hangs",
            funcargs={"tmp_path": tmp_path},
        )
    )


def test_forge_watchdog_worktree_diagnostics_find_run_worktrees(tmp_path: Path) -> None:
    run_dir = tmp_path / "repo" / ".alysis" / "runs" / "run-1"
    worktree_repo = run_dir / "worktrees" / "T01" / "repo"
    worktree_repo.mkdir(parents=True)
    failed_cleanup_marker = worktree_repo.parent / "failed_cleanup.json"
    failed_cleanup_marker.write_text('{"reason":"cleanup failed"}\n', encoding="utf-8")

    diagnostics = forge_conftest._forge_worktree_diagnostics(_request_for_tmp_path(tmp_path))

    assert diagnostics
    entry = diagnostics[0]
    assert entry["kind"] == "worktrees"
    assert entry["task_id"] == "T01"
    assert entry["path"] == str(worktree_repo)
    assert entry["exists"] is True
    assert entry["failed_cleanup_marker"] == str(failed_cleanup_marker)


def test_forge_watchdog_payload_includes_cleanup_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(forge_conftest.sys, "__stderr__", stream)
    monkeypatch.setattr(
        forge_conftest.faulthandler,
        "dump_traceback",
        lambda *, file, all_threads: file.write("stack traces\n"),
    )

    run_dir = tmp_path / "repo" / ".alysis" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "active_execution.lock.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "mode": "forge_swarm",
                "kind": "lock",
                "pid": 12345,
                "owner_token": "sk-test-watchdog-secret-value",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "worktrees" / "T01" / "repo").mkdir(parents=True)

    forge_conftest._write_forge_watchdog_diagnostics(
        request=_request_for_tmp_path(tmp_path),
        timeout_s=0.01,
    )

    output = stream.getvalue()
    assert "Alysis Code Forge test watchdog timeout" in output
    assert '"nodeid": "tests/test_forge_exec.py::test_hangs"' in output
    assert '"active_threads"' in output
    assert '"child_processes"' in output
    assert '"live_mcp_stdio_transports"' in output
    assert '"run_locks"' in output
    assert '"owner_token": "[redacted]"' in output
    assert "sk-test-watchdog-secret-value" not in output
    assert '"worktrees"' in output
    assert "stack traces" in output


def test_forge_watchdog_redacts_child_process_command_lines() -> None:
    assert (
        forge_conftest._redact_watchdog_text("python tool.py --api-key sk-test-secret-value")
        == "python tool.py --api-key [REDACTED]"
    )
    assert forge_conftest._redact_watchdog_text("Bearer abcdefghijklmnop") == "Bearer [REDACTED]"
