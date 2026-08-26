from __future__ import annotations

from alysis_code.swarm_scheduler import Batch, Schedule, compute_schedule


def _task(
    task_id: str,
    *,
    estimated_files: list[str] | None = None,
    write_scope: list[str] | None = None,
    parallel_group: str = "",
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id,
        "status": "planned",
        "dependencies": [],
        "estimated_files": estimated_files or [],
        "write_scope": write_scope or [],
        "parallel_group": parallel_group,
    }


def _batch_for(schedule: Schedule, task_id: str) -> Batch:
    return next(batch for batch in schedule.batches if task_id in batch.task_ids)


def test_scheduler_builds_conservative_batches_from_estimated_files() -> None:
    tasks = [
        {"id": "T00", "title": "base", "status": "done", "dependencies": [], "estimated_files": []},
        {
            "id": "T01",
            "title": "A",
            "status": "planned",
            "dependencies": ["T00"],
            "estimated_files": ["src/a.py"],
        },
        {
            "id": "T02",
            "title": "B",
            "status": "planned",
            "dependencies": ["T00"],
            "estimated_files": ["src/b.py"],
        },
        {
            "id": "T03",
            "title": "C",
            "status": "planned",
            "dependencies": ["T00"],
            "estimated_files": ["src/a.py"],
        },
        {
            "id": "T04",
            "title": "D",
            "status": "planned",
            "dependencies": ["T00"],
            "estimated_files": [],
        },
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [c.task_id for c in schedule.runnable] == ["T01", "T02", "T03", "T04"]
    assert [b.task_ids for b in schedule.batches] == [["T01", "T02"], ["T03"], ["T04"]]


def test_scheduler_allows_parallel_batching_from_explicit_write_scope() -> None:
    tasks = [
        _task("T01", write_scope=["src/a.py"]),
        _task("T02", write_scope=["src/b.py"]),
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [b.task_ids for b in schedule.batches] == [["T01", "T02"]]
    assert "claimed scopes disjoint" in schedule.batches[0].reasons[1]


def test_scheduler_ignores_synthetic_python_support_file_overlaps_when_batching() -> None:
    tasks = [
        _task("T01", write_scope=["taskboard/report.py", "tests/test_report.py"]),
        _task("T02", write_scope=["taskboard/summary.py", "tests/test_summary.py"]),
        _task("T03", write_scope=["taskboard/owners.py", "tests/test_owners.py"]),
        _task("T04", write_scope=["taskboard/cli.py", "tests/test_cli.py"]),
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=3,
        max_tasks=None,
        retry_failed=False,
    )

    assert [b.task_ids for b in schedule.batches] == [["T01", "T02", "T03"], ["T04"]]
    assert "claimed scopes disjoint" in schedule.batches[0].reasons[1]
    assert "claimed scopes disjoint" in schedule.batches[0].reasons[2]


def test_scheduler_keeps_explicit_support_file_overlaps_as_conflicts() -> None:
    tasks = [
        _task("T01", write_scope=["taskboard/report.py", "taskboard/__init__.py"]),
        _task("T02", write_scope=["taskboard/summary.py", "taskboard/__init__.py"]),
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [b.task_ids for b in schedule.batches] == [["T01"], ["T02"]]
    assert "claimed scope overlap: taskboard/__init__.py" in _batch_for(schedule, "T02").reasons[0]


def test_scheduler_runs_missing_scope_metadata_alone() -> None:
    tasks = [
        _task("T01", estimated_files=["src/a.py"]),
        _task("T02"),
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [b.task_ids for b in schedule.batches] == [["T01"], ["T02"]]
    assert "missing claimed write scope metadata" in schedule.batches[1].reasons[0]


def test_scheduler_runs_ambiguous_scope_alone() -> None:
    tasks = [
        _task("T01", estimated_files=["src/a.py"], write_scope=["src/"]),
        _task("T02", estimated_files=["src/b.py"], write_scope=["src/b.py"]),
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [b.task_ids for b in schedule.batches] == [["T02"], ["T01"]]
    assert "ambiguous claimed scope" in schedule.batches[1].reasons[0]
    assert "src/**" in schedule.batches[1].reasons[0]


def test_scheduler_uses_union_of_estimated_files_and_write_scope_for_conflicts() -> None:
    tasks = [
        _task("T01", estimated_files=["src/a.py"], write_scope=["README.md"]),
        _task("T02", estimated_files=["src/b.py"], write_scope=["README.md"]),
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [b.task_ids for b in schedule.batches] == [["T01"], ["T02"]]
    t02_batch = _batch_for(schedule, "T02")
    assert "blocked by T01" in t02_batch.reasons[0]
    assert "claimed scope overlap: README.md" in t02_batch.reasons[0]


def test_scheduler_reasons_remain_informative_for_parallel_group_blocks() -> None:
    tasks = [
        _task("T01", write_scope=["src/a.py"], parallel_group="ui"),
        _task("T02", write_scope=["src/b.py"], parallel_group="ui"),
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [b.task_ids for b in schedule.batches] == [["T01"], ["T02"]]
    assert "same parallel_group: ui" in _batch_for(schedule, "T02").reasons[0]


def test_scheduler_filters_by_dependency_and_only_ids() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "A",
            "status": "planned",
            "dependencies": [],
            "estimated_files": ["a.py"],
        },
        {
            "id": "T02",
            "title": "B",
            "status": "planned",
            "dependencies": ["T99"],
            "estimated_files": ["b.py"],
        },
        {
            "id": "T03",
            "title": "C",
            "status": "planned",
            "dependencies": [],
            "estimated_files": ["c.py"],
        },
    ]
    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
        only_ids={"T01", "T02"},
    )

    assert [c.task_id for c in schedule.runnable] == ["T01"]
    assert schedule.skipped["T02"].startswith("dependency")
    assert schedule.skipped["T03"] == "filtered by --only"


def test_scheduler_respects_inferred_ordered_dependency_without_mutating_plan() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Implement auth API",
            "description": "Add the auth endpoint.",
            "status": "planned",
            "dependencies": [],
            "estimated_files": ["src/auth.py"],
            "write_scope": ["src/auth.py"],
        },
        {
            "id": "T02",
            "title": "Then update auth docs",
            "description": "Then document auth usage after the previous work lands.",
            "status": "planned",
            "dependencies": [],
            "estimated_files": ["docs/auth.md"],
            "write_scope": ["docs/auth.md"],
        },
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [c.task_id for c in schedule.runnable] == ["T01"]
    assert schedule.skipped["T02"] == "dependency not done: T01 (planned)"
    assert [b.task_ids for b in schedule.batches] == [["T01"]]
    assert tasks[1]["dependencies"] == []


def test_scheduler_allows_implementation_after_ready_red_regression_precursor() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Add red regression tests for slug behavior",
            "description": "Tests capture the current bug and fail before fix.",
            "status": "ready_for_merge",
            "dependencies": [],
            "estimated_files": ["tests/test_slug.py"],
            "write_scope": ["tests/test_slug.py"],
        },
        {
            "id": "T02",
            "title": "Fix slug behavior",
            "description": "Implement the slugger change.",
            "status": "planned",
            "dependencies": ["T01"],
            "estimated_files": ["slugger.py"],
            "write_scope": ["slugger.py"],
        },
    ]

    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )

    assert [c.task_id for c in schedule.runnable] == ["T02"]
    assert "T02" not in schedule.skipped


def test_scheduler_treats_legacy_todo_as_planned_and_respects_retry_failed() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "A",
            "status": "todo",
            "dependencies": [],
            "estimated_files": ["a.py"],
        },
        {
            "id": "T02",
            "title": "B",
            "status": "failed",
            "dependencies": [],
            "estimated_files": ["b.py"],
        },
    ]
    schedule_no_retry = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=False,
    )
    assert [c.task_id for c in schedule_no_retry.runnable] == ["T01"]
    assert schedule_no_retry.skipped["T02"] == "status not runnable: failed"

    schedule_retry = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=True,
    )
    assert [c.task_id for c in schedule_retry.runnable] == ["T01", "T02"]


def test_scheduler_skips_changes_requested_unless_retry_enabled() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Review blocked",
            "status": "changes_requested",
            "dependencies": [],
            "estimated_files": ["a.py"],
        }
    ]
    schedule_default = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=1,
        max_tasks=None,
        retry_failed=False,
    )
    assert schedule_default.runnable == []
    assert schedule_default.skipped["T01"] == "status not runnable: changes_requested"

    schedule_retry = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=1,
        max_tasks=None,
        retry_failed=False,
        retry_changes_requested=True,
    )
    assert [c.task_id for c in schedule_retry.runnable] == ["T01"]


def test_scheduler_respects_max_attempts() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Too many attempts",
            "status": "failed",
            "attempts": 3,
            "dependencies": [],
            "estimated_files": ["a.py"],
        },
        {
            "id": "T02",
            "title": "Still eligible",
            "status": "failed",
            "attempts": 2,
            "dependencies": [],
            "estimated_files": ["b.py"],
        },
    ]
    schedule = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=2,
        max_tasks=None,
        retry_failed=True,
        max_attempts=3,
    )
    assert [c.task_id for c in schedule.runnable] == ["T02"]
    assert schedule.skipped["T01"] == "attempt limit reached: 3 >= 3"


def test_scheduler_allows_retry_for_verify_failed_when_retry_failed_enabled() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Verify failed",
            "status": "verify_failed",
            "dependencies": [],
            "estimated_files": ["a.py"],
        }
    ]
    schedule_no_retry = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=1,
        max_tasks=None,
        retry_failed=False,
    )
    assert schedule_no_retry.runnable == []

    schedule_retry = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=1,
        max_tasks=None,
        retry_failed=True,
    )
    assert [c.task_id for c in schedule_retry.runnable] == ["T01"]


def test_scheduler_allows_retry_for_candidate_rejected_when_retry_failed_enabled() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Candidate rejected",
            "status": "candidate_rejected",
            "dependencies": [],
            "estimated_files": ["a.py"],
        }
    ]
    schedule_no_retry = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=1,
        max_tasks=None,
        retry_failed=False,
    )
    assert schedule_no_retry.runnable == []
    assert schedule_no_retry.skipped["T01"] == "status not runnable: candidate_rejected"

    schedule_retry = compute_schedule(
        base_branch="main",
        tasks=tasks,
        parallel=1,
        max_tasks=None,
        retry_failed=True,
    )
    assert [c.task_id for c in schedule_retry.runnable] == ["T01"]
