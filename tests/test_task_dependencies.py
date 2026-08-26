from __future__ import annotations

from alysis_code.task_dependencies import infer_ordered_predecessor_dependency


def test_nextjs_task_name_does_not_infer_ordered_dependency() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Update billing API",
            "description": "Change billing behavior.",
            "status": "planned",
            "dependencies": [],
            "acceptance_criteria": [],
            "estimated_files": ["src/billing.py"],
            "write_scope": ["src/billing.py"],
        },
        {
            "id": "T02",
            "title": "Fix Next.js routing",
            "description": "Repair route handling in the web app.",
            "status": "planned",
            "dependencies": [],
            "acceptance_criteria": [],
            "estimated_files": ["web/app/page.tsx"],
            "write_scope": ["web/app/page.tsx"],
        },
    ]

    assert infer_ordered_predecessor_dependency(tasks=tasks, task=tasks[1]) is None


def test_next_task_phrase_still_infers_ordered_dependency() -> None:
    tasks = [
        {
            "id": "T01",
            "title": "Implement auth API",
            "description": "Add the auth endpoint.",
            "status": "planned",
            "dependencies": [],
            "acceptance_criteria": [],
            "estimated_files": ["src/auth.py"],
            "write_scope": ["src/auth.py"],
        },
        {
            "id": "T02",
            "title": "Next task updates auth docs",
            "description": "Document auth usage after the previous work lands.",
            "status": "planned",
            "dependencies": [],
            "acceptance_criteria": [],
            "estimated_files": ["docs/auth.md"],
            "write_scope": ["docs/auth.md"],
        },
    ]

    dependency = infer_ordered_predecessor_dependency(tasks=tasks, task=tasks[1])

    assert dependency is not None
    assert dependency.depends_on == "T01"
