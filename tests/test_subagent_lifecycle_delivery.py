from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from test_subagent_workspaces import _EditingChildSession, _Harness, _repo
from test_subagents import (
    _build_main_tools,
    _FakeSubSession,
    _FakeSubSessionStore,
    _readonly_subagent_tools,
)

from alysis_code import agent_loop
from alysis_code.subagents import SubagentDefinition


def _finished(scheduler: Any, run_id: str) -> None:
    child = scheduler._children[run_id]
    child.completion.result(timeout=5)
    child.worker_bookkeeping_completion.result(timeout=5)


def _reader_registry() -> dict[str, SubagentDefinition]:
    return {
        "explorer": SubagentDefinition(
            name="explorer",
            description="Read source",
            system_prompt="Inspect the assignment.",
            mode="readonly",
            allow_tools=("fs_read",),
            allow_workspace_writes=False,
        )
    }


def test_completion_delivery_retries_is_bounded_and_does_not_consume_late_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    releases = [threading.Event(), threading.Event()]
    reports = ["src/context.py:1 preserves context.\n" * 180, "Η δεύτερη ανάλυση ολοκληρώθηκε."]
    created = 0

    class Child(_FakeSubSession):
        def __init__(self, index: int) -> None:
            super().__init__(
                tools=_readonly_subagent_tools(),
                messages=[{"role": "assistant", "content": reports[index]}],
                session_id=f"child-{index}",
            )
            self.index = index

        def run_turn(self, task: str, *, cancellation_token: Any = None) -> int:
            assert releases[self.index].wait(timeout=5)
            return super().run_turn(task, cancellation_token=cancellation_token)

    def create_child(**_kwargs: Any) -> Child:
        nonlocal created
        child = Child(created)
        created += 1
        return child

    monkeypatch.setattr(agent_loop, "create_session", create_child)
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry=_reader_registry()
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        first = tools["subagent_spawn"].run({"name": "explorer", "task": "Inspect context."})
        second = tools["subagent_spawn"].run({"name": "explorer", "task": "Inspect tests."})
        assert scheduler.pending_completion_notifications() == []
        scheduler.acknowledge_completion_notifications([first["run_id"]])
        releases[0].set()
        _finished(scheduler, first["run_id"])
        notifications = scheduler.pending_completion_notifications(max_items=1)
        assert notifications == scheduler.pending_completion_notifications(max_items=1)
        assert len(notifications) == 1
        notification = notifications[0]
        assert notification["run_id"] == first["run_id"]
        assert notification["status"] == "success"
        assert notification["status_scope"] == "child_execution"
        assert notification["report_truncated"] is True
        assert len(notification["report"]) == 4000
        assert notification["full_result"] == {
            "tool": "subagent_wait",
            "arguments": {"run_id": first["run_id"]},
        }
        releases[1].set()
        _finished(scheduler, second["run_id"])
        scheduler.acknowledge_completion_notifications([first["run_id"], first["run_id"]])
        assert [item["run_id"] for item in scheduler.pending_completion_notifications()] == [
            second["run_id"]
        ]
        full = tools["subagent_wait"].run({"run_id": first["run_id"]})
        assert full["results"][first["run_id"]]["result"] == reports[0].strip()
        tools["subagent_wait"].run({"run_id": second["run_id"]})
        assert scheduler.pending_completion_notifications() == []
    finally:
        for release in releases:
            release.set()
        scheduler.shutdown(cancel_pending=True)


def test_failed_completion_is_deliverable_and_retrievable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry=_reader_registry()
    )
    launcher = tools["subagent_run"].run.__self__
    scheduler = launcher.child_scheduler

    def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("worker could not start")

    monkeypatch.setattr(launcher, "run_registered", fail)
    try:
        spawned = tools["subagent_spawn"].run({"name": "explorer", "task": "Inspect source."})
        child = scheduler._children[spawned["run_id"]]
        child.worker_bookkeeping_completion.result(timeout=5)
        notification = scheduler.pending_completion_notifications()[0]
        assert notification["status"] == "failed"
        assert notification["error"] == "worker could not start"
        scheduler.acknowledge_completion_notifications([spawned["run_id"]])
        assert scheduler.pending_completion_notifications() == []
        result = tools["subagent_wait"].run({"run_id": spawned["run_id"]})
        assert result["results"][spawned["run_id"]]["status"] == "failed"
    finally:
        scheduler.shutdown(cancel_pending=True)


def test_cancelled_completion_is_delivered_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry=_reader_registry()
    )
    launcher = tools["subagent_run"].run.__self__
    scheduler = launcher.child_scheduler

    def cancelled(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=5)
        assert kwargs["cancellation_token"].is_cancelled
        return {"status": "cancelled", "error": "Stopped at parent request."}

    monkeypatch.setattr(launcher, "run_registered", cancelled)
    try:
        spawned = tools["subagent_spawn"].run({"name": "explorer", "task": "Inspect source."})
        assert started.wait(timeout=5)
        tools["subagent_cancel"].run({"run_id": spawned["run_id"]})
        release.set()
        _finished(scheduler, spawned["run_id"])
        notification = scheduler.pending_completion_notifications()[0]
        assert notification["status"] == "cancelled"
        scheduler.acknowledge_completion_notifications([spawned["run_id"]])
        assert scheduler.pending_completion_notifications() == []
        result = tools["subagent_wait"].run({"run_id": spawned["run_id"]})
        assert result["results"][spawned["run_id"]]["status"] == "cancelled"
    finally:
        release.set()
        scheduler.shutdown(cancel_pending=True)


@pytest.mark.parametrize("launch_kind", ["background", "sync"])
def test_successful_followups_preserve_history_candidate_and_single_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_kind: str
) -> None:
    root = _repo(tmp_path)
    roots: list[Path] = []
    histories: list[list[dict[str, Any]]] = []
    launch_options: list[dict[str, Any]] = []
    reports = ["Initial change is complete.", "Review finding addressed.", "Final check completed."]

    class Child(_EditingChildSession):
        def __init__(self, child_root: Path, index: int) -> None:
            super().__init__(root=child_root, content=f"candidate-{index}\n")
            self.messages = []
            self.index = index
            self.store = _FakeSubSessionStore(session_id=f"followup-{index}")

        def run_turn(self, task: str, *, cancellation_token: Any = None) -> int:
            histories.append(list(self.messages))
            if self.index:
                assert (self.root / "app.txt").read_text() == f"candidate-{self.index - 1}\n"
            result = super().run_turn(task, cancellation_token=cancellation_token)
            self.messages.append({"role": "assistant", "content": reports[self.index]})
            self.store._events = [
                {"type": "user_message", "payload": {"content": task}},
                {"type": "assistant_message", "payload": {"content": reports[self.index]}},
                {"type": "final", "payload": {"content": reports[self.index]}},
            ]
            return result

    def create_child(**kwargs: Any) -> Child:
        launch_options.append(kwargs)
        roots.append(Path(kwargs["root"]))
        return Child(roots[-1], len(roots) - 1)

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        subagent_registry={
            "general": SubagentDefinition(
                name="general",
                description="Bounded implementation",
                system_prompt="Complete the task.",
                mode="auto",
                allow_tools=("fs_read", "fs_write"),
                allow_workspace_writes=True,
            )
        },
        deny_write_prefixes=["protected"],
    )
    scheduler = harness.launcher.child_scheduler
    try:
        args = {"name": "general", "task": "Make the initial change.", "workspace_view": "isolated"}
        if launch_kind == "sync":
            first = harness.tools["subagent_run"].run(args)
        else:
            first = harness.tools["subagent_spawn"].run(args)
            _finished(scheduler, first["run_id"])
            scheduler.acknowledge_completion_notifications([first["run_id"]])
        assert scheduler.unapplied_isolated_results()
        missing_task = harness.tools["subagent_resume"].run({"run_id": first["run_id"]})
        assert missing_task["error_code"] == "subagent_followup_requires_task"
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = list(
                executor.map(
                    lambda _: harness.tools["subagent_resume"].run(
                        {"run_id": first["run_id"], "task": "Address the review finding."}
                    ),
                    range(2),
                )
            )
        resumed = next(item for item in attempts if "error" not in item)
        rejected = next(item for item in attempts if "error" in item)
        assert rejected["error_code"] == "subagent_already_continued"
        assert rejected["continuation_run_id"] == resumed["run_id"]
        _finished(scheduler, resumed["run_id"])
        assert roots[0] == roots[1]
        assert reports[0] in str(histories[1])
        assert harness.launcher.workspace_provider.get(first["run_id"]) is None
        second = harness.tools["subagent_resume"].run(
            {"run_id": resumed["run_id"], "task": "Perform the final check."}
        )
        _finished(scheduler, second["run_id"])
        assert roots[0] == roots[2]
        assert str(histories[2]).count(reports[0]) == 1
        assert reports[1] in str(histories[2])
        assert all(options["deny_write_prefixes"] == ["protected"] for options in launch_options)
        assert (root / "app.txt").read_text() == "base\n"
        applied = harness.tools["subagent_apply"].run({"run_id": second["run_id"]})
        assert applied["ok"] is True
        assert (root / "app.txt").read_text() == "candidate-2\n"
        assert scheduler.unapplied_isolated_results() == []
    finally:
        harness.close()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX special-file support")
def test_incomplete_capture_is_degraded_before_terminal_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)

    roots: list[Path] = []

    class UnsupportedChild(_EditingChildSession):
        def run_turn(self, task: str, *, cancellation_token: Any = None) -> int:
            result = super().run_turn(task, cancellation_token=cancellation_token)
            output = self.root / "runtime.pipe"
            if len(roots) == 1:
                os.mkfifo(output)
            else:
                assert task == "Replace the unsupported pipe with a portable text file."
                output.unlink()
                output.write_text("portable output\n")
            return result

    def create_child(**kwargs: Any) -> UnsupportedChild:
        roots.append(Path(kwargs["root"]))
        return UnsupportedChild(root=roots[-1])

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.run()
        assert result["status"] == "degraded"
        assert result["error_code"] == "incomplete_workspace_patch"
        assert result["workspace"]["no_changes"] is False
        assert result["material_patch_complete"] is False
        assert result["omitted_material_paths"] == ["runtime.pipe"]
        assert result["candidate_worktree_retained"] is True
        ends = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event["type"] == "subagent_end"
        ]
        assert len(ends) == 1
        assert ends[0]["status"] == "degraded"
        assert ends[0]["material_patch_complete"] is False
        applied = harness.tools["subagent_apply"].run(
            {"run_id": result["run_id"], "acknowledge_incomplete": True}
        )
        assert applied["error_code"] == "incomplete_workspace_patch"
        assert (root / "app.txt").read_text() == "base\n"
        assert harness.launcher.child_scheduler.unapplied_isolated_results()
        missing_task = harness.tools["subagent_resume"].run({"run_id": result["run_id"]})
        assert missing_task["error_code"] == "subagent_followup_requires_task"
        recovered = harness.tools["subagent_resume"].run(
            {
                "run_id": result["run_id"],
                "task": "Replace the unsupported pipe with a portable text file.",
            }
        )
        waited = harness.tools["subagent_wait"].run({"run_id": recovered["run_id"]})
        assert waited["results"][recovered["run_id"]]["status"] == "success"
        assert roots[0] == roots[1]
        assert harness.tools["subagent_apply"].run({"run_id": recovered["run_id"]})["ok"] is True
        assert (root / "runtime.pipe").read_text() == "portable output\n"
    finally:
        harness.close()


def test_resume_registration_failure_restores_candidate_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    roots: list[Path] = []

    def create_child(**kwargs: Any) -> _EditingChildSession:
        roots.append(Path(kwargs["root"]))
        return _EditingChildSession(root=roots[-1], content=f"revision-{len(roots)}\n")

    harness = _Harness(
        tmp_path=tmp_path, root=root, create_child=create_child, monkeypatch=monkeypatch
    )
    try:
        first = harness.run()
        source_id = first["run_id"]
        append = harness.store.append
        failed = False

        def fail_registration(event_type: str, payload: dict[str, Any], **kwargs: Any) -> Any:
            nonlocal failed
            if (
                not failed
                and event_type == "subagent_state"
                and payload.get("resumed_from") == source_id
            ):
                failed = True
                raise OSError("injected registration append failure")
            return append(event_type, payload, **kwargs)

        monkeypatch.setattr(harness.store, "append", fail_registration)
        rejected = harness.tools["subagent_resume"].run(
            {"run_id": source_id, "task": "Revise the existing candidate."}
        )
        assert rejected["error_code"] == "subagent_resume_setup_failed"
        assert "injected registration append failure" in rejected["error"]
        assert rejected["source_workspace_restored"] is True
        assert rejected["retained_worktree_run_id"] == source_id
        assert len(roots) == 1
        restored = harness.launcher.workspace_provider.get(source_id)
        assert restored is not None and restored.worktree_path == roots[0]
        assert (roots[0] / "app.txt").read_text() == "revision-1\n"
        scheduler = harness.launcher.child_scheduler
        assert scheduler._children[source_id].continuation_run_id is None
        retried = harness.tools["subagent_resume"].run(
            {"run_id": source_id, "task": "Revise the existing candidate."}
        )
        _finished(scheduler, retried["run_id"])
        assert roots[0] == roots[1]
        assert harness.tools["subagent_apply"].run({"run_id": retried["run_id"]})["ok"] is True
        assert (root / "app.txt").read_text() == "revision-2\n"
    finally:
        harness.close()


def test_resume_does_not_silently_replace_a_missing_retained_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        monkeypatch=monkeypatch,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
    )
    try:
        first = harness.run()
        provider = harness.launcher.workspace_provider
        assert (
            provider.reattach_for_resume(first["run_id"], "retained-external-owner")["ok"] is True
        )
        rejected = harness.tools["subagent_resume"].run(
            {"run_id": first["run_id"], "task": "Revise the existing candidate."}
        )
        assert rejected["error_code"] == "subagent_resume_worktree_unavailable"
        assert len(harness.launcher.child_scheduler._children) == 1
        retained = provider.get("retained-external-owner")
        assert retained is not None and retained.worktree_path.exists()
        assert (root / "app.txt").read_text() == "base\n"
    finally:
        harness.close()
