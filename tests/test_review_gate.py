from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from alysis_code.config import AppConfig
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.review_gate import ReviewError, parse_patch_changed_files, review_task


def _write_patch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )


def _extract_prompt_payload(messages: object) -> dict[str, object]:
    assert isinstance(messages, list)
    user_prompt = str(messages[1]["content"])
    marker = "Input context:\n"
    schema_marker = "\n\nRequired JSON schema:\n"
    start = user_prompt.index(marker) + len(marker)
    end = user_prompt.index(schema_marker)
    return json.loads(user_prompt[start:end])


def _git_spaced_file_patch(repo: Path) -> str:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    target = repo / "foo bar.txt"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "foo bar.txt"], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True
    )
    target.write_text("new\n", encoding="utf-8")
    cp = subprocess.run(
        ["git", "diff", "--", "foo bar.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return cp.stdout


def test_review_task_writes_json_and_markdown_artifacts(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Implement app change",
        description="Update app logic",
        acceptance_criteria=["app.py updated", "tests pass"],
        estimated_files=["src/app.py"],
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    _write_patch(patch_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")

    payload = {
        "approved": True,
        "confidence": "high",
        "blocking_issues": [],
        "non_blocking_issues": [{"title": "n1", "details": "d1", "suggested_fix": "s1"}],
        "acceptance_criteria_checks": [
            {"criterion": "app.py updated", "status": "met", "evidence": "diff shows updates"}
        ],
        "summary": "Looks good.",
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.review_gate.OpenAICompatClient", FakeClient)

    outcome = review_task(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
    )
    assert outcome.approved is True
    assert outcome.confidence == "high"
    assert outcome.json_path.exists()
    assert outcome.markdown_path.exists()

    review_json = json.loads(outcome.json_path.read_text(encoding="utf-8"))
    assert review_json["approved"] is True
    assert "src/app.py" in review_json["changed_files"]

    review_md = outcome.markdown_path.read_text(encoding="utf-8")
    assert "Review: " in review_md
    assert "Approved: yes" in review_md


def test_review_task_uses_role_model_override(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Review me", description="desc")
    plan["role_models"] = {"review": "plan-review-model"}
    save_plan(paths, plan)

    task_id = str(task["id"])
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    _write_patch(patch_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")

    payload = {
        "approved": True,
        "confidence": "high",
        "blocking_issues": [],
        "non_blocking_issues": [],
        "acceptance_criteria_checks": [],
        "summary": "ok",
    }
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured["model"] = kwargs["model"]
            captured["temperature"] = kwargs["temperature"]
            captured["timeout_s"] = kwargs["timeout_s"]

        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["messages"] = kwargs["messages"]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.review_gate.OpenAICompatClient", FakeClient)

    outcome = review_task(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model", review_temperature=0.11, llm_timeout_s=22.0),
        api_key_override="k",
    )
    assert outcome.approved is True
    assert captured["model"] == "plan-review-model"
    assert captured["temperature"] == 0.11
    assert captured["timeout_s"] == 22.0
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "strict senior code reviewer" in str(messages[0]["content"]).lower()
    assert "definition of done" in str(messages[0]["content"]).lower()
    assert "review checklist" in str(messages[1]["content"]).lower()


def test_review_task_warns_for_fallback_model_metadata(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Review me", description="desc")
    save_plan(paths, plan)

    task_id = str(task["id"])
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    _write_patch(patch_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")

    payload = {
        "approved": True,
        "confidence": "high",
        "blocking_issues": [],
        "non_blocking_issues": [],
        "acceptance_criteria_checks": [],
        "summary": "ok",
    }
    warnings_seen: list[str] = []

    def _warn(message: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        _ = args, kwargs
        warnings_seen.append(str(message))

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("warnings.warn", _warn)
    monkeypatch.setattr("alysis_code.review_gate.OpenAICompatClient", FakeClient)

    outcome = review_task(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="unknown-model-xyz"),
        api_key_override="k",
    )

    assert outcome.approved is True
    assert warnings_seen
    assert "unknown-model-xyz" in warnings_seen[0]


def test_review_task_strict_model_metadata_policy_fails_before_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Review me", description="desc")
    save_plan(paths, plan)

    task_id = str(task["id"])
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    _write_patch(patch_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("OpenAICompatClient should not be constructed in strict mode")

    monkeypatch.setattr("alysis_code.review_gate.OpenAICompatClient", FakeClient)

    with pytest.raises(ReviewError, match="model_metadata_policy=strict"):
        review_task(
            paths=paths,
            plan=load_plan(paths),
            task=task,
            cfg=AppConfig(model="unknown-model-xyz", model_metadata_policy="strict"),
            api_key_override="k",
        )


def test_review_task_enforces_conservative_approval_policy(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Review policy task",
        description="desc",
        acceptance_criteria=["criterion-1"],
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    _write_patch(patch_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")

    payload = {
        "approved": True,
        "confidence": "high",
        "blocking_issues": [{"title": "b1", "details": "d1", "suggested_fix": "s1"}],
        "non_blocking_issues": [],
        "acceptance_criteria_checks": [
            {"criterion": "criterion-1", "status": "met", "evidence": "ok"}
        ],
        "summary": "looks fine",
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.review_gate.OpenAICompatClient", FakeClient)

    outcome = review_task(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
    )
    assert outcome.approved is False

    review_json = json.loads(outcome.json_path.read_text(encoding="utf-8"))
    assert review_json["approved"] is False
    titles = [str(item.get("title")) for item in review_json["blocking_issues"]]
    assert "Approval policy violation" in titles


def test_review_task_includes_structured_verification_payload_in_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Review structured verification", description="desc")
    save_plan(paths, plan)

    task_id = str(task["id"])
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    worker_result_path = paths.execution_dir / "worker_results" / f"{task_id}.json"
    _write_patch(patch_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")
    worker_result_path.parent.mkdir(parents=True, exist_ok=True)
    worker_result_path.write_text(
        json.dumps(
            {
                "changed_files": ["src/app.py"],
                "verify_summary": "verification passed (1/1)",
                "verify_failed": False,
                "verify_payload": {
                    "summary": "verification passed (1/1)",
                    "all_passed": True,
                    "failed_commands": [],
                    "command_results": [
                        {
                            "command": "go test ./... && go build ./...",
                            "effective_command": "go test ./... && go build ./...",
                            "exit_code": 0,
                            "ok": True,
                            "real_execution": None,
                            "fallback_used": False,
                            "output_preview": "? pkg/example [no test files]\n",
                        }
                    ],
                    "fallback_used": False,
                    "fallback_count": 0,
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    payload = {
        "approved": True,
        "confidence": "high",
        "blocking_issues": [],
        "non_blocking_issues": [],
        "acceptance_criteria_checks": [],
        "summary": "ok",
    }
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["messages"] = kwargs["messages"]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.review_gate.OpenAICompatClient", FakeClient)

    outcome = review_task(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
    )
    assert outcome.approved is True

    prompt_payload = _extract_prompt_payload(captured["messages"])
    verification = prompt_payload["verification"]
    assert isinstance(verification, dict)
    assert verification["summary"] == "verification passed (1/1)"
    assert verification["all_passed"] is True
    assert verification["evidence_source"] == "worker_result"
    command_results = verification["command_results"]
    assert isinstance(command_results, list)
    assert command_results[0]["command"] == "go test ./... && go build ./..."
    assert command_results[0]["exit_code"] == 0
    assert command_results[0]["ok"] is True
    assert command_results[0]["real_execution"] is None


def test_review_task_falls_back_to_report_excerpt_for_older_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Older artifact review", description="desc")
    save_plan(paths, plan)

    task_id = str(task["id"])
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    _write_patch(patch_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "# report\nVerification passed for `pytest -q`.\n",
        encoding="utf-8",
    )

    payload = {
        "approved": True,
        "confidence": "high",
        "blocking_issues": [],
        "non_blocking_issues": [],
        "acceptance_criteria_checks": [],
        "summary": "ok",
    }
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["messages"] = kwargs["messages"]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.review_gate.OpenAICompatClient", FakeClient)

    outcome = review_task(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
    )
    assert outcome.approved is True

    prompt_payload = _extract_prompt_payload(captured["messages"])
    verification = prompt_payload["verification"]
    assert isinstance(verification, dict)
    assert verification["evidence_source"] == "report_excerpt"
    assert "Verification passed for `pytest -q`." in str(prompt_payload["report_excerpt"])


def test_parse_patch_changed_files_handles_spaces(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    patch_text = _git_spaced_file_patch(repo)
    assert parse_patch_changed_files(patch_text) == ["foo bar.txt"]
