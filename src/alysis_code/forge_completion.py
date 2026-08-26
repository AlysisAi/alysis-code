"""Post-execution completion report for Forge swarm runs.

``/execute plan`` historically ended with a one-line headline plus a pointer at
``swarm_summary.md`` — the run's actual results (what each worker built, where
the files landed, whether the output is trustworthy, and how to try it) stayed
buried in run artifacts nothing surfaced. This module turns those artifacts
into one user-facing markdown report, deterministically on the host side: no
model call, so it works identically for every provider and costs nothing.

Inputs are read best-effort from the run directory (``plan.json`` statuses,
``execution/worker_results/*.json``, the per-task
``knowledge_capture/<task>/<ts>/assistant_message.md`` worker narratives) so a
partially-written run still produces an honest, if sparser, report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .verification_repair import TASK_STATUS_COMPLETED_UNVERIFIED

_DONE_STATES = frozenset({"done"})
_NOOP_STATES = frozenset({"already_satisfied"})
# Its own bucket on purpose: counted as finished (the work landed and was kept), but
# rendered with the caveat, because nothing authoritative checked it.
_UNVERIFIED_STATES = frozenset({TASK_STATUS_COMPLETED_UNVERIFIED})
_FAILURE_STATES = frozenset(
    {
        "failed",
        "verify_failed",
        "candidate_rejected",
        "changes_requested",
        "merge_conflict",
        "blocked_integration",
        "blocked",
        "interrupted",
        "cancelled",
    }
)
_OBSOLETE_STATES = frozenset({"superseded", "invalidated"})
_MAX_FILES_SHOWN = 12
_MAX_NOTE_CHARS = 140
_MAX_REASON_CHARS = 160
_MAX_HINTS = 3
_RUN_SCRIPT_PRIORITY = ("dev", "start", "serve", "preview")


def _canonical_status(value: object) -> str:
    status = str(value or "").strip().lower()
    try:
        from .swarm_scheduler import canonical_task_status

        return canonical_task_status(status)
    except Exception:  # noqa: BLE001
        return status or "planned"


def _current_branch(root: Path) -> str:
    try:
        from .git_ops import current_branch

        return str(current_branch(root)).strip()
    except Exception:  # noqa: BLE001
        return ""


def _load_worker_results(paths: Any) -> dict[str, dict[str, Any]]:
    """Index ``execution/worker_results/*.json`` payloads by task id."""
    execution_dir = getattr(paths, "execution_dir", None)
    if execution_dir is None:
        return {}
    results_dir = Path(execution_dir) / "worker_results"
    indexed: dict[str, dict[str, Any]] = {}
    try:
        entries = sorted(results_dir.glob("*.json"))
    except Exception:  # noqa: BLE001
        return {}
    for entry in entries:
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        task_id = str(payload.get("task_id") or "").strip()
        if task_id:
            indexed[task_id] = payload
    return indexed


def _worker_note(worker_payload: dict[str, Any]) -> str:
    """First meaningful line of the worker's own final answer, if captured."""
    capture_dir = str(worker_payload.get("knowledge_capture_artifact_dir") or "").strip()
    if not capture_dir:
        return ""
    message_path = Path(capture_dir) / "assistant_message.md"
    try:
        text = message_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "(none captured)":
            continue
        if line.startswith(("#", "```", "---", "KNOWLEDGE")):
            continue
        line = line.lstrip("-*> ").strip()
        if not line:
            continue
        if len(line) > _MAX_NOTE_CHARS:
            line = line[: _MAX_NOTE_CHARS - 1].rstrip() + "…"
        return line
    return ""


def _failure_reason(task: dict[str, Any], worker_payload: dict[str, Any]) -> str:
    for candidate in (
        task.get("last_error"),
        worker_payload.get("failure_reason"),
        worker_payload.get("error"),
        worker_payload.get("summary"),
    ):
        reason = " ".join(str(candidate or "").split())
        if reason:
            if len(reason) > _MAX_REASON_CHARS:
                reason = reason[: _MAX_REASON_CHARS - 1].rstrip() + "…"
            return reason
    return "see the task report for details"


def _shallowest_existing(root: Path, rel_paths: list[str]) -> Path | None:
    for rel in sorted(rel_paths, key=lambda item: (item.count("/"), item)):
        candidate = root / Path(rel)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _package_json_hint(pkg_path: Path) -> str:
    try:
        payload = json.loads(pkg_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return ""
    script_name = next(
        (name for name in _RUN_SCRIPT_PRIORITY if str(scripts.get(name) or "").strip()),
        "",
    )
    if not script_name:
        return ""
    run_cmd = "npm start" if script_name == "start" else f"npm run {script_name}"
    pkg_dir = pkg_path.parent
    needs_install = not (pkg_dir / "node_modules").is_dir()
    command = f"npm install && {run_cmd}" if needs_install else run_cmd
    return f'Node app: `cd "{pkg_dir}"` then `{command}` (package.json `{script_name}` script)'


def detect_run_hints(root: Path, changed_files: list[str] | None = None) -> list[str]:
    """Grounded "try it" suggestions: only commands whose target files exist.

    Prefers files this run actually changed over pre-existing ones, and always
    names absolute directories — the workspace root the swarm merged into is
    not necessarily the directory the user's shell sits in.
    """
    root = Path(root)
    normalized = [str(item).replace("\\", "/").strip("/") for item in (changed_files or [])]
    hints: list[str] = []

    index_candidates = [rel for rel in normalized if rel.rsplit("/", 1)[-1].lower() == "index.html"]
    index_path = _shallowest_existing(root, index_candidates)
    if index_path is None:
        try:
            if (root / "index.html").is_file():
                index_path = root / "index.html"
        except OSError:
            index_path = None
    if index_path is not None:
        serve_dir = index_path.parent
        hints.append(
            f"Static site: open `{index_path}` in a browser, or serve it — "
            f'`cd "{serve_dir}"` then `python -m http.server 8000` and visit '
            "http://localhost:8000"
        )

    pkg_candidates = [rel for rel in normalized if rel.rsplit("/", 1)[-1] == "package.json"]
    pkg_path = _shallowest_existing(root, pkg_candidates)
    if pkg_path is None:
        try:
            if (root / "package.json").is_file():
                pkg_path = root / "package.json"
        except OSError:
            pkg_path = None
    if pkg_path is not None:
        pkg_hint = _package_json_hint(pkg_path)
        if pkg_hint:
            hints.append(pkg_hint)

    return hints[:_MAX_HINTS]


def _task_rows(
    tasks: list[dict[str, Any]],
    worker_results: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], int, int, int, int]:
    """Render per-task lines; return (rows, merged files, done, noop, failed, remaining)."""
    rows: list[str] = []
    merged_files: list[str] = []
    done = noop = failed = remaining = 0
    for task in tasks:
        if not isinstance(task, dict):
            remaining += 1
            continue
        task_id = str(task.get("id") or "").strip() or "?"
        title = " ".join(str(task.get("title") or "").split()) or "(untitled task)"
        status = _canonical_status(task.get("status"))
        worker_payload = worker_results.get(task_id) or {}
        changed = [
            str(item).strip()
            for item in (worker_payload.get("changed_files") or [])
            if str(item).strip()
        ]
        if status in _DONE_STATES:
            done += 1
            merged_files.extend(changed)
            detail = f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed"
            note = _worker_note(worker_payload)
            row = f"- ✓ **{task_id} · {title}** — {detail}"
            if note:
                row += f" — {note}"
            if bool(worker_payload.get("verify_failed")):
                row += " ⚠ its checks failed but were tolerated (verify mode: warn)"
            rows.append(row)
        elif status in _UNVERIFIED_STATES:
            done += 1
            merged_files.extend(changed)
            detail = f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed"
            note = _worker_note(worker_payload)
            row = (
                f"- ✓ **{task_id} · {title}** — {detail} — ⚠ kept but UNVERIFIED: "
                "no authoritative verification command exists for this workspace"
            )
            if note:
                row += f" — {note}"
            rows.append(row)
        elif status in _NOOP_STATES:
            noop += 1
            rows.append(
                f"- ○ **{task_id} · {title}** — reported no changes needed "
                "(already satisfied); nothing was merged for it"
            )
        elif status in _FAILURE_STATES:
            failed += 1
            rows.append(
                f"- ✗ **{task_id} · {title}** — {status}: {_failure_reason(task, worker_payload)}"
            )
        elif status in _OBSOLETE_STATES:
            rows.append(f"- · **{task_id} · {title}** — {status}")
        else:
            remaining += 1
            rows.append(f"- … **{task_id} · {title}** — {status or 'planned'}")
    return rows, merged_files, done, noop, failed, remaining


def build_forge_completion_report(
    *,
    paths: Any,
    plan: dict[str, Any],
    run_status: str = "",
    run_clean: bool | None = None,
    exit_code: int = 0,
) -> str:
    """Render the post-run answer shown to the user after ``/execute plan``."""
    tasks = list(plan.get("tasks") or [])
    worker_results = _load_worker_results(paths)
    rows, merged_files, done, noop, failed, remaining = _task_rows(tasks, worker_results)
    total = len(tasks)
    root = Path(getattr(paths, "root", "."))

    finished = done + noop
    if total <= 0:
        headline = "### Forge execution finished — no tasks were in the plan"
    elif failed == 0 and remaining == 0 and exit_code == 0:
        headline = f"### Forge execution complete — {finished}/{total} tasks finished"
    else:
        parts = [f"{finished} finished"]
        if failed:
            parts.append(f"{failed} failed")
        if remaining:
            parts.append(f"{remaining} not finished")
        headline = "### Forge execution finished with issues — " + " · ".join(parts)

    lines: list[str] = [headline, ""]

    unique_files = sorted(dict.fromkeys(merged_files))
    branch = _current_branch(root)
    if unique_files:
        where = f"branch `{branch}` in `{root}`" if branch else f"`{root}`"
        lines.append(f"**Where the work landed:** merged into {where}.")
    elif total > 0:
        lines.append(
            "⚠ **No files were changed by this run.** If you expected new files "
            "(for example a website), nothing was actually written to "
            f"`{root}` — check the per-task lines and reports below."
        )
    lines.append("")

    if rows:
        lines.append("**Tasks:**")
        lines.extend(rows)
        lines.append("")

    if unique_files:
        shown = unique_files[:_MAX_FILES_SHOWN]
        extra = len(unique_files) - len(shown)
        listing = ", ".join(f"`{item}`" for item in shown)
        if extra > 0:
            listing += f" (+{extra} more)"
        lines.append(f"**Files changed ({len(unique_files)}):** {listing}")
        lines.append("")

    warn_verified = sum(
        1
        for payload in worker_results.values()
        if bool(payload.get("verify_failed")) and bool(payload.get("success"))
    )
    if run_clean is False and run_status:
        lines.append(
            f"⚠ **Verification:** run status is `{run_status}` — some checks failed, "
            "were tolerated, or never ran; treat completed tasks as unverified."
        )
        lines.append("")
    elif warn_verified:
        lines.append(
            f"⚠ **Verification:** {warn_verified} task(s) merged with failing checks "
            "(verify mode: warn)."
        )
        lines.append("")

    if unique_files:
        hints = detect_run_hints(root, unique_files)
        if hints:
            lines.append("**Try it:**")
            lines.extend(f"- {hint}" for hint in hints)
            lines.append("")

    execution_dir = getattr(paths, "execution_dir", None)
    if execution_dir is not None:
        summary_path = Path(execution_dir) / "swarm_summary.md"
        reports_dir = getattr(paths, "execution_reports_dir", Path(execution_dir) / "reports")
        lines.append(f"**Details:** `{summary_path}` · per-task reports in `{reports_dir}`")

    return "\n".join(lines).strip()


__all__ = ["build_forge_completion_report", "detect_run_hints"]
