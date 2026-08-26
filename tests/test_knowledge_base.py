from __future__ import annotations

import json
import os
from pathlib import Path

import alysis_code.knowledge_base as kb
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.knowledge_base import (
    _write_text_atomic,
    is_effectively_accepted_task_attempt,
    load_knowledge_entry,
    load_knowledge_index,
    rebuild_knowledge_index,
    write_decision_entry,
    write_fact_entry,
    write_issue_entry,
    write_task_attempt_entry,
    write_task_attempt_resolution_entry,
)


def _remove_effective_status_from_index(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload.get("entries") or []:
        if isinstance(entry, dict):
            entry.pop("effective_status", None)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_knowledge_entries_are_append_only_and_index_rebuilds_from_disk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)

    created_at = "2026-03-12T12:00:00Z"
    first = write_task_attempt_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        result="success",
        summary="First attempt succeeded.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        created_at=created_at,
    )
    second = write_task_attempt_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        result="failure",
        summary="Second attempt failed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest failed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        created_at=created_at,
    )
    issue = write_issue_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        title="T01: strict verification failed",
        summary="Host verification blocked task completion.",
        paths_in_scope=["src/parser.py"],
        tags=["verification_failure"],
    )

    assert first.file_path is not None
    assert second.file_path is not None
    assert first.file_path != second.file_path
    assert first.file_path.exists()
    assert second.file_path.exists()
    assert issue.file_path is not None

    loaded_issue = load_knowledge_entry(issue.file_path)
    assert loaded_issue.kind == "issue"
    assert loaded_issue.status == "open"
    assert loaded_issue.paths == ("src/parser.py",)

    index = rebuild_knowledge_index(paths)
    assert paths.knowledge_index_path.exists()
    assert len(index.entries) == 3
    assert {entry.kind for entry in index.entries} == {"task_attempt", "issue"}
    assert {entry.run_id for entry in index.entries} == {paths.run_id}
    assert len({first.id, second.id, issue.id}) == 3
    assert index.invalid_entries == ()


def test_task_attempt_uses_task_scope_when_changed_files_are_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Update parser docs",
        estimated_files=["docs/parser.md"],
    )
    task["write_scope"] = ["docs/parser.md"]
    save_plan(paths, plan)

    entry = write_task_attempt_entry(
        paths=paths,
        task=task,
        source="swarm_worker",
        result="success",
        summary="No git diff was available.",
        changed_files=[],
        verify_summary=None,
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    assert entry.paths == ("docs/parser.md",)


def test_task_attempt_publish_is_atomic_and_uses_hidden_temp_file(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Atomic publish", estimated_files=["src/parser.py"])
    save_plan(paths, plan)

    original_replace = os.replace
    observed: dict[str, str] = {}

    def fake_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        src_path = Path(src)
        dst_path = Path(dst)
        if dst_path.suffix == ".md":
            observed["src_name"] = src_path.name
            observed["dst_name"] = dst_path.name
            assert not dst_path.exists()
            assert src_path.exists()
            assert src_path.suffix == ".tmp"
            assert not src_path.name.endswith(".md")
        original_replace(src, dst)

    monkeypatch.setattr(kb.os, "replace", fake_replace)

    entry = write_task_attempt_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        result="success",
        summary="Atomic publish test.",
        changed_files=["src/parser.py"],
        verify_summary=None,
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    assert entry.file_path is not None
    assert entry.file_path.exists()
    assert observed["dst_name"].endswith(".md")
    assert observed["src_name"].endswith(".tmp")


def test_rebuild_knowledge_index_skips_invalid_entries_and_records_diagnostics(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Valid parser task", estimated_files=["src/parser.py"])
    save_plan(paths, plan)

    write_task_attempt_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        result="success",
        summary="Valid entry.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    invalid_path = paths.knowledge_issues_dir / str(task["id"]) / "broken_entry.md"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_text('---\nkind: "issue"\nthis-is-not-valid\n', encoding="utf-8")

    index = rebuild_knowledge_index(paths)

    assert len(index.entries) == 1
    assert len(index.invalid_entries) == 1
    assert index.invalid_entries[0].knowledge_file_path.endswith("broken_entry.md")
    assert "ValueError" in index.invalid_entries[0].error
    payload = json.loads(paths.knowledge_index_path.read_text(encoding="utf-8"))
    assert payload["invalid_entry_count"] == 1
    assert payload["invalid_entries"][0]["knowledge_file_path"].endswith("broken_entry.md")


def test_write_text_atomic_uses_unique_temp_files(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "index.json"
    original_replace = os.replace
    replace_sources: list[str] = []

    def fake_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        replace_sources.append(Path(src).name)
        original_replace(src, dst)

    monkeypatch.setattr(kb.os, "replace", fake_replace)

    _write_text_atomic(path, '{"version": 1}\n')
    _write_text_atomic(path, '{"version": 2}\n')

    assert len(replace_sources) == 2
    assert replace_sources[0] != replace_sources[1]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_rebuild_knowledge_index_derives_effective_status_from_resolution_entries(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    open_issue = kb.write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Initial integration failure.",
        paths_in_scope=["src/parser.py"],
        status="open",
        signature="integration_gate_v1:test",
    )
    kb.write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_002",
        source="integration_gate",
        title="batch_002: integration verification passed",
        summary="Later integration pass.",
        paths_in_scope=["src/parser.py"],
        status="resolved",
        signature="integration_gate_v1:test",
        resolves=[open_issue.id],
    )

    index = rebuild_knowledge_index(paths)

    indexed_open_issue = next(entry for entry in index.entries if entry.id == open_issue.id)
    assert indexed_open_issue.status == "open"
    assert indexed_open_issue.effective_status == "resolved"


def test_task_attempt_effective_acceptance_is_derived_from_resolution_entries(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Parser swarm task",
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)

    pending_attempt = write_task_attempt_entry(
        paths=paths,
        task=task,
        source="swarm_worker",
        result="success",
        summary="Worker completed successfully.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="pending",
    )
    write_task_attempt_resolution_entry(
        paths=paths,
        task=task,
        source="swarm_orchestrator",
        acceptance_state="accepted",
        resolved_attempt_id=pending_attempt.id,
        summary="Worker result was accepted after merge.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    index = rebuild_knowledge_index(paths)

    indexed_pending = next(entry for entry in index.entries if entry.id == pending_attempt.id)
    resolution_entry = next(
        entry
        for entry in index.entries
        if entry.kind == "task_attempt" and pending_attempt.id in entry.resolves
    )
    assert indexed_pending.status == "pending"
    assert indexed_pending.result == "success"
    assert indexed_pending.effective_status == "accepted"
    assert is_effectively_accepted_task_attempt(indexed_pending.effective_status)
    assert resolution_entry.status == "accepted"
    assert resolution_entry.result is None


def test_write_fact_and_decision_entries_round_trip_into_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Capture parser knowledge",
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)
    capture_artifact = paths.execution_dir / "knowledge_capture" / "T01" / "capture" / "summary.md"
    capture_artifact.parent.mkdir(parents=True, exist_ok=True)
    capture_artifact.write_text("capture summary\n", encoding="utf-8")

    fact_entry = write_fact_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        title="Parser retries use a bounded backoff",
        summary="Observed retry logic in src/parser.py uses a bounded backoff.",
        paths_in_scope=["src/parser.py"],
        capture_artifact_path=capture_artifact,
        tags=["parser", "retry"],
    )
    active_decision = write_decision_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Use bounded parser retry backoff",
        summary="Keep the bounded backoff behavior for parser retries.",
        status="active",
        paths_in_scope=["src/parser.py"],
        capture_artifact_path=capture_artifact,
        tags=["parser", "retry"],
        created_at="2026-03-12T12:00:00Z",
    )
    invalidated_decision = write_decision_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Use bounded parser retry backoff",
        summary="Superseded after later execution.",
        status="invalidated",
        paths_in_scope=["src/parser.py"],
        capture_artifact_path=capture_artifact,
        tags=["parser", "retry"],
        created_at="2026-03-12T13:00:00Z",
    )

    index = rebuild_knowledge_index(paths)

    indexed_fact = next(entry for entry in index.entries if entry.id == fact_entry.id)
    assert indexed_fact.kind == "fact"
    assert indexed_fact.capture_artifact_path is not None
    assert indexed_fact.capture_artifact_path.endswith("summary.md")
    indexed_active_decision = next(
        entry for entry in index.entries if entry.id == active_decision.id
    )
    indexed_invalidated_decision = next(
        entry for entry in index.entries if entry.id == invalidated_decision.id
    )
    assert indexed_active_decision.decision_key == "parser-retry-backoff"
    assert indexed_active_decision.status == "active"
    assert indexed_active_decision.effective_status == "invalidated"
    assert indexed_invalidated_decision.effective_status == "invalidated"


def test_decision_entries_without_decision_key_keep_raw_effective_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    legacy_path = paths.knowledge_decisions_dir / "legacy" / "legacy_decision.md"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        "\n".join(
            [
                "---",
                'kind: "decision"',
                'id: "legacy-decision-001"',
                'title: "Legacy decision"',
                'created_at: "2026-03-12T10:00:00Z"',
                'task_id: "T00"',
                'source: "legacy_import"',
                'status: "active"',
                "paths:",
                '  - "src/parser.py"',
                "related_tasks:",
                "tags:",
                "report_path: null",
                "patch_path: null",
                "verify_artifact_path: null",
                "budget_artifact_path: null",
                "session_artifact_dir: null",
                "signature: null",
                "resolves:",
                "capture_artifact_path: null",
                "---",
                "# Decision: Legacy decision",
                "",
                "- Status: `active`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    index = rebuild_knowledge_index(paths)

    indexed = next(entry for entry in index.entries if entry.id == "legacy-decision-001")
    assert indexed.status == "active"
    assert indexed.effective_status == "active"


def test_load_knowledge_index_backfills_run_id_from_knowledge_file_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Parser task", estimated_files=["src/parser.py"])
    save_plan(paths, plan)
    write_task_attempt_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        result="success",
        summary="Parser task succeeded.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    index = rebuild_knowledge_index(paths)
    payload = json.loads(paths.knowledge_index_path.read_text(encoding="utf-8"))
    payload["entries"][0].pop("run_id", None)
    paths.knowledge_index_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    loaded = load_knowledge_index(paths)

    assert loaded.entries
    assert loaded.entries[0].run_id == index.entries[0].run_id == paths.run_id


def test_load_knowledge_index_rebuilds_current_schema_cache_missing_effective_status(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    open_issue = kb.write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Initial integration failure.",
        paths_in_scope=["src/parser.py"],
        status="open",
        signature="integration_gate_v1:test",
    )
    kb.write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_002",
        source="integration_gate",
        title="batch_002: integration verification passed",
        summary="Later integration pass.",
        paths_in_scope=["src/parser.py"],
        status="resolved",
        signature="integration_gate_v1:test",
        resolves=[open_issue.id],
    )
    rebuild_knowledge_index(paths)
    _remove_effective_status_from_index(paths.knowledge_index_path)

    loaded = load_knowledge_index(paths)

    indexed_open_issue = next(entry for entry in loaded.entries if entry.id == open_issue.id)
    assert indexed_open_issue.effective_status == "resolved"
    payload = json.loads(paths.knowledge_index_path.read_text(encoding="utf-8"))
    assert all(
        "effective_status" in entry
        for entry in payload.get("entries") or []
        if isinstance(entry, dict)
        and str(entry.get("kind") or "").strip() in {"issue", "decision", "task_attempt"}
    )


def test_load_knowledge_index_keeps_invalidated_decision_truth_when_cache_is_incomplete(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Decision task", estimated_files=["src/parser.py"])
    save_plan(paths, plan)
    active = write_decision_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Use bounded parser retry backoff",
        summary="Original active decision.",
        status="active",
        paths_in_scope=["src/parser.py"],
        created_at="2026-03-12T12:00:00Z",
    )
    write_decision_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Use bounded parser retry backoff",
        summary="Later invalidation.",
        status="invalidated",
        paths_in_scope=["src/parser.py"],
        created_at="2026-03-12T13:00:00Z",
    )
    rebuild_knowledge_index(paths)
    _remove_effective_status_from_index(paths.knowledge_index_path)

    loaded = load_knowledge_index(paths)

    indexed_active = next(entry for entry in loaded.entries if entry.id == active.id)
    assert indexed_active.status == "active"
    assert indexed_active.effective_status == "invalidated"


def test_load_knowledge_index_keeps_task_attempt_effective_state_when_cache_is_incomplete(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Parser swarm task", estimated_files=["src/parser.py"])
    save_plan(paths, plan)
    pending_attempt = write_task_attempt_entry(
        paths=paths,
        task=task,
        source="swarm_worker",
        result="success",
        summary="Worker completed successfully.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="pending",
    )
    write_task_attempt_resolution_entry(
        paths=paths,
        task=task,
        source="swarm_orchestrator",
        acceptance_state="rejected",
        resolved_attempt_id=pending_attempt.id,
        summary="Worker result was rejected after merge/apply failed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest failed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    rebuild_knowledge_index(paths)
    _remove_effective_status_from_index(paths.knowledge_index_path)

    loaded = load_knowledge_index(paths)

    indexed_pending = next(entry for entry in loaded.entries if entry.id == pending_attempt.id)
    assert indexed_pending.status == "pending"
    assert indexed_pending.effective_status == "rejected"
    assert not is_effectively_accepted_task_attempt(indexed_pending.effective_status)
