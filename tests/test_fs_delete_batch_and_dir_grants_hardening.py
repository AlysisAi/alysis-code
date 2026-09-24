"""Grant-path hardening: dot segments can never ride a folder grant.

``pkg/..`` starts with ``pkg/`` textually, so a naive prefix match would let a
stored ``(fs_delete, pkg/)`` grant auto-approve a request that resolves outside
the granted folder. The tool layer would still refuse to delete a directory,
but the approval layer must not green-light it in the first place.
"""

from __future__ import annotations

from alysis_code.approval_scope import (
    approval_dir_grant_candidate,
    exact_file_set_scope,
    request_matches_dir_grants,
)
from alysis_code.surface.types import ApprovalRequest


def _delete_request(files: list[str]) -> ApprovalRequest:
    return ApprovalRequest(
        kind="fs_delete",
        reason="file deletion requires confirmation",
        preview="Delete file",
        files=files,
        allow_for_session_scope=exact_file_set_scope(files, operation="fs_delete"),
    )


def test_dot_segments_never_match_a_grant() -> None:
    grants = {("fs_delete", "pkg/")}
    assert request_matches_dir_grants(_delete_request(["pkg/.."]), grants) is False
    assert request_matches_dir_grants(_delete_request(["pkg/."]), grants) is False
    assert request_matches_dir_grants(_delete_request(["pkg/sub/../x.pyc"]), grants) is False
    assert request_matches_dir_grants(_delete_request(["pkg//x.pyc"]), grants) is False
    # Sanity: a plain in-folder file still matches.
    assert request_matches_dir_grants(_delete_request(["pkg/x.pyc"]), grants) is True


def test_dot_segments_never_produce_a_candidate() -> None:
    assert approval_dir_grant_candidate(_delete_request(["pkg/.."])) is None
    assert approval_dir_grant_candidate(_delete_request(["pkg/./x.pyc"])) is None
    assert approval_dir_grant_candidate(_delete_request(["./pkg/x.pyc"])) is None
    assert approval_dir_grant_candidate(_delete_request(["pkg/x.pyc"])) == "pkg/"


# --------- Codex review on PR #26: mixed batches + swarm outcome honesty ---------


def test_mixed_batch_ordinary_files_still_require_write_approval(tmp_path):
    """A batch mixing sensitive and ordinary files must cover the ordinary
    files with a write approval of their own - the sensitive prompt lists only
    the sensitive findings, and nothing may ride along unapproved."""
    import io

    from rich.console import Console

    from alysis_code.agent_loop import build_tools
    from alysis_code.config import AppConfig
    from alysis_code.session_store import SessionStore
    from alysis_code.surface.types import ApprovalDecision

    (tmp_path / "pkg").mkdir()
    for name in ("a.pyc", "b.pyc", "creds.pem"):
        (tmp_path / "pkg" / name).write_bytes(b"x")

    class _Recorder:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(allow=True)

    surface = _Recorder()
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=SessionStore(
            enabled=False,
            sessions_dir=tmp_path / "sessions",
            session_id="mixed-batch-test",
            cwd=str(tmp_path),
            repo_root=str(tmp_path),
        ),
        mode="auto",
        yes=False,
        cfg=AppConfig(model="test-model"),
        non_interactive=False,
    )

    result = tools["fs_delete"].run({"paths": ["pkg/a.pyc", "pkg/creds.pem", "pkg/b.pyc"]})

    approved_files = [sorted(request.files) for request in surface.requests]
    # One sensitive prompt (creds only) + one write approval (ordinary only).
    assert ["pkg/creds.pem"] in approved_files
    assert ["pkg/a.pyc", "pkg/b.pyc"] in approved_files
    # Every deleted file appeared in some approval.
    covered = {path for files in approved_files for path in files}
    assert covered == {"pkg/a.pyc", "pkg/b.pyc", "pkg/creds.pem"}
    assert result["deleted_count"] == 3
    assert not any((tmp_path / "pkg" / name).exists() for name in ("a.pyc", "b.pyc", "creds.pem"))


def test_swarm_outcome_reports_not_run_when_gates_skipped():
    """Run-level skip honesty: a final integration gate that proceeded without
    executing anything must record verification_status not_run, not passed."""
    from types import SimpleNamespace

    from alysis_code.swarm_orchestrator import _compute_swarm_run_outcome

    def _outcome(results):
        return _compute_swarm_run_outcome(
            plan={"tasks": []},
            integration_results=results,
            worker_verification_warnings=False,
            review_blocked=False,
            remote_blocked_any=False,
            integration_blocked=False,
            only="t1",
            max_tasks=None,
            dry_run=False,
        )

    def _gate(**kwargs) -> SimpleNamespace:
        base = {
            "passed": True,
            "phase": "post_merge",
            "mode": "warn",
            "policy_outcome": "passed",
            "batch_label": "batch-1",
            "summary": "stub",
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    skipped_final = _gate(phase="final_repo", policy_outcome="not_run")
    executed_final = _gate(phase="final_repo", policy_outcome="passed")
    skipped_mid = _gate(policy_outcome="not_run")

    assert _outcome([skipped_final]).verification_status == "not_run"
    assert _outcome([skipped_mid]).verification_status == "not_run"
    assert _outcome([executed_final]).verification_status == "passed"
    assert _outcome([skipped_mid, executed_final]).verification_status == "passed"
