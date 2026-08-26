from __future__ import annotations

import json
from pathlib import Path

from alysis_code.config import AppConfig
from alysis_code.failure_category import FailureCategory
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.integration_gate import (
    integration_issue_signature_for_commands,
    normalize_integration_verify_mode,
    record_integration_failure_knowledge,
    record_integration_resolution_knowledge,
    resolve_integration_verify_commands,
    resolve_integration_verify_mode,
    run_integration_gate,
)
from alysis_code.knowledge_base import (
    load_knowledge_entry,
    load_knowledge_index,
    write_issue_entry_for_task_id,
)
from alysis_code.repo_scan import scan_workspace
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult
from alysis_code.workspace_context import resolve_workspace_context


def test_resolve_integration_verify_mode_uses_cli_or_config() -> None:
    cfg = AppConfig(model="test-model", integration_verify_mode="strict")
    assert (
        resolve_integration_verify_mode(cfg=AppConfig(model="test-model"), integration_verify=None)
        == "warn"
    )
    assert resolve_integration_verify_mode(cfg=cfg, integration_verify=None) == "strict"
    assert resolve_integration_verify_mode(cfg=cfg, integration_verify="warn") == "warn"
    assert normalize_integration_verify_mode("off") == "off"


def test_resolve_integration_verify_commands_prefers_dedicated_then_verify_fallback() -> None:
    cfg = AppConfig(
        model="test-model",
        verify_commands=["pytest -q"],
        integration_verify_commands=["python -m pytest tests/integration -q"],
    )
    resolved = resolve_integration_verify_commands(
        cfg=cfg,
        integration_verify_cmd=None,
        verify_cmd=["pytest -q tests/custom.py"],
    )
    assert resolved.commands == ("python -m pytest tests/integration -q",)
    assert resolved.source == "config.integration_verify_commands"

    # An explicitly configured generic preset is honored when no workspace is
    # available to scan, but it is labeled as a generic-preset fallback rather
    # than an authoritative repo-specific configuration.
    fallback = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q"]),
        integration_verify_cmd=None,
        verify_cmd=None,
    )
    assert fallback.commands == ("pytest -q",)
    assert fallback.source == "config.verify_commands_generic_preset_fallback"


def test_resolve_integration_verify_commands_uses_noop_when_only_generic_default_is_available(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model"),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands_fallback"


def test_integration_verify_resolution_treats_generic_configured_preset_as_fallback_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."]),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands_fallback"


def test_integration_verify_resolution_treats_pytest_plus_plain_ruff_check_as_generic_fallback_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check"]),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands_fallback"


def test_integration_verify_resolution_treats_uv_run_pytest_and_ruff_check_as_generic_fallback_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model", verify_commands=["uv run pytest -q", "ruff check"]),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands_fallback"


def test_integration_verify_resolution_treats_uv_run_pytest_and_uv_run_ruff_check_as_generic_fallback_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(
            model="test-model", verify_commands=["uv run pytest -q", "uv run ruff check"]
        ),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands_fallback"


def test_integration_verify_resolution_treats_module_invoked_generic_preset_as_fallback_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(
            model="test-model",
            verify_commands=["uv run python -m pytest -q", "uv run python -m ruff check ."],
        ),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands_fallback"


def test_integration_verify_resolution_treats_windows_launcher_generic_preset_as_fallback_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(
            model="test-model",
            verify_commands=[
                r"C:\Python311\python.exe -m pytest -q",
                r"C:\Python311\python.exe -m ruff check .",
            ],
        ),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands_fallback"


def test_resolve_integration_verify_commands_prefers_repo_inference_over_generic_default(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "packages" / "web").mkdir(parents=True)
    (repo / "packages" / "web" / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    (repo / "services" / "orders").mkdir(parents=True)
    (repo / "services" / "orders" / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>\n",
        encoding="utf-8",
    )

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model"),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == (
        "npm --prefix packages/web test",
        "mvn -f services/orders/pom.xml test",
    )
    assert resolved.source == "repo_scan.likely_test_commands_fallback"


def test_integration_verify_resolution_prefers_repo_inference_over_generic_configured_preset(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."]),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ("npm test",)
    assert resolved.source == "repo_scan.likely_test_commands_fallback"


def test_resolve_integration_verify_commands_infers_plain_python_repo_without_manifest(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model"),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=repo,
    )

    assert resolved.commands == ("pytest -q",)
    assert resolved.source == "repo_scan.likely_test_commands_fallback"


def test_resolve_integration_verify_commands_rescans_candidate_root_when_repo_scan_is_stale(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    stale_scan = scan_workspace(context=resolve_workspace_context(base))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    (candidate / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (candidate / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model"),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=candidate,
        repo_scan=stale_scan,
    )

    assert resolved.commands == ("npm test", "pytest -q")
    assert resolved.source == "repo_scan.likely_test_commands_fallback"


def test_resolve_integration_verify_commands_preserves_focus_when_rescanning_candidate_root(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    service = base / "service"
    (service / "app").mkdir(parents=True)
    (service / "tests").mkdir()
    (service / "app" / "config.py").write_text("def load_mode():\n    return 'dev'\n")
    (service / "tests" / "test_config.py").write_text(
        "from app.config import load_mode\n\n\ndef test_load_mode():\n    assert load_mode() == 'dev'\n",
        encoding="utf-8",
    )
    focused_scan = scan_workspace(context=resolve_workspace_context(service))

    candidate = tmp_path / "candidate"
    candidate_service = candidate / "service"
    (candidate_service / "app").mkdir(parents=True)
    (candidate_service / "tests").mkdir()
    (candidate_service / "app" / "config.py").write_text(
        "def load_mode():\n    return 'prod'\n",
        encoding="utf-8",
    )
    (candidate_service / "tests" / "test_config.py").write_text(
        "from app.config import load_mode\n\n\ndef test_load_mode():\n    assert load_mode() == 'prod'\n",
        encoding="utf-8",
    )

    resolved = resolve_integration_verify_commands(
        cfg=AppConfig(model="test-model"),
        integration_verify_cmd=None,
        verify_cmd=None,
        root=candidate,
        repo_scan=focused_scan,
    )

    assert resolved.commands == ("PYTHONPATH=service pytest -q service",)
    assert resolved.source == "repo_scan.likely_test_commands_fallback"


def test_run_integration_gate_writes_artifacts_and_payloads(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    def fake_verify(*, root, commands, artifact_path, cfg, timeout_s=900):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verify artifact\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    exit_code=1,
                    output="failure\n",
                    stdout="stdout line\n",
                    stderr="stderr line\n",
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.integration_gate.run_task_verification", fake_verify)

    result = run_integration_gate(
        paths=paths,
        cfg=AppConfig(model="test-model", verify_commands=["python -m pytest tests/unit -q"]),
        batch_index=1,
        mode="warn",
        merged_task_ids=["T01"],
        merged_paths=["src/parser.py"],
    )

    assert result.passed is False
    assert result.commands_path.exists()
    assert result.stdout_path.exists()
    assert result.stderr_path.exists()
    assert result.summary_path.exists()
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert payload["command_source"] == "config.verify_commands_fallback"
    assert payload["phase"] == "post_merge"
    assert payload["merged_task_ids"] == ["T01"]
    assert payload["merged_paths"] == ["src/parser.py"]
    assert payload["failure_category"] == FailureCategory.VERIFICATION_FAILED.value
    assert payload["policy_outcome"] == "failed_tolerated_by_warn_policy"
    assert payload["tolerated_failure"] is True
    assert payload["verified_root"] == "."
    assert "Policy Outcome: `failed_tolerated_by_warn_policy`" in result.summary_path.read_text(
        encoding="utf-8"
    )
    assert payload["verify_artifact_path"].endswith("execution/integration/batch_001/verify.txt")
    assert "stdout line" in result.stdout_path.read_text(encoding="utf-8")
    assert "stderr line" in result.stderr_path.read_text(encoding="utf-8")


def test_run_integration_gate_uses_repo_inferred_commands_when_config_is_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "packages" / "web").mkdir(parents=True)
    (repo / "packages" / "web" / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    paths = create_plan_run(repo)
    captured: dict[str, object] = {}

    def fake_verify(*, root, commands, artifact_path, cfg, timeout_s=900):  # type: ignore[no-untyped-def]
        captured["root"] = root
        captured["commands"] = list(commands)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verify artifact\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    exit_code=0,
                    output="ok\n",
                    stdout="stdout line\n",
                    stderr="",
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.integration_gate.run_task_verification", fake_verify)

    result = run_integration_gate(
        paths=paths,
        cfg=AppConfig(model="test-model"),
        batch_index=1,
        mode="warn",
        merged_task_ids=["T01"],
        merged_paths=["packages/web/package.json"],
        phase="pre_merge_candidate",
    )

    assert captured["root"] == repo.resolve()
    assert captured["commands"] == ["npm --prefix packages/web test"]
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert payload["command_source"] == "repo_scan.likely_test_commands_fallback"
    assert payload["phase"] == "pre_merge_candidate"
    assert payload["verified_root"] == "."
    summary_text = result.summary_path.read_text(encoding="utf-8")
    assert "Phase: `pre-merge candidate`" in summary_text
    assert "## Batch Tasks" in summary_text


def test_record_integration_failure_writes_issue_and_summary(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(plan, title="Implement parser retry", estimated_files=["src/parser.py"])
    save_plan(first_run, plan)

    def fake_verify(*, root, commands, artifact_path, cfg, timeout_s=900):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verify artifact\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    exit_code=1,
                    output="failure\n",
                    stdout="",
                    stderr="stderr line\n",
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.integration_gate.run_task_verification", fake_verify)

    result = run_integration_gate(
        paths=first_run,
        cfg=AppConfig(model="test-model", verify_commands=["python -m pytest tests/unit -q"]),
        batch_index=1,
        mode="strict",
        merged_task_ids=[str(task["id"])],
        merged_paths=["src/parser.py"],
    )
    issue, summary_path = record_integration_failure_knowledge(paths=first_run, result=result)

    assert issue.source == "integration_gate"
    assert issue.related_tasks == (str(task["id"]),)
    assert issue.paths == ("src/parser.py",)
    assert issue.file_path is not None
    loaded = load_knowledge_entry(issue.file_path)
    assert loaded.status == "open"
    assert summary_path.exists()
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "Integration Issues" in summary_text
    assert "integration verification failed" in summary_text
    assert loaded.signature == integration_issue_signature_for_commands(result.commands)


def test_integration_issue_signature_is_stable_for_normalized_commands() -> None:
    baseline = integration_issue_signature_for_commands(
        ("pytest   -q", "python -m pytest tests/integration -q")
    )
    assert baseline == integration_issue_signature_for_commands(
        ("pytest -q", "python   -m   pytest tests/integration -q")
    )
    assert baseline != integration_issue_signature_for_commands(("pytest -q tests/unit",))


def test_record_integration_resolution_closes_matching_open_issues(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(plan, title="Implement parser retry", estimated_files=["src/parser.py"])
    save_plan(first_run, plan)

    def failing_verify(*, root, commands, artifact_path, cfg, timeout_s=900):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verify artifact\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    exit_code=1,
                    output="failure\n",
                    stdout="",
                    stderr="stderr line\n",
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.integration_gate.run_task_verification", failing_verify)
    failed_result = run_integration_gate(
        paths=first_run,
        cfg=AppConfig(model="test-model", verify_commands=["python -m pytest tests/unit -q"]),
        batch_index=1,
        mode="warn",
        merged_task_ids=[str(task["id"])],
        merged_paths=["src/parser.py"],
    )
    failed_issue, _ = record_integration_failure_knowledge(paths=first_run, result=failed_result)

    second_run = create_plan_run(repo)

    def passing_verify(*, root, commands, artifact_path, cfg, timeout_s=900):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verify artifact\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    exit_code=0,
                    output="ok\n",
                    stdout="stdout line\n",
                    stderr="",
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.integration_gate.run_task_verification", passing_verify)
    passed_result = run_integration_gate(
        paths=second_run,
        cfg=AppConfig(model="test-model", verify_commands=["python -m pytest tests/unit -q"]),
        batch_index=2,
        mode="warn",
        merged_task_ids=[str(task["id"])],
        merged_paths=["src/parser.py"],
    )
    resolution, summary_path = record_integration_resolution_knowledge(
        paths=second_run, result=passed_result
    )

    assert resolution is not None
    assert resolution.resolves == (failed_issue.id,)
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "No open integration issues." in summary_text
    index = load_knowledge_index(second_run, rebuild=True)
    failed_entry = next(entry for entry in index.entries if entry.id == failed_issue.id)
    assert failed_entry.status == "open"
    assert failed_entry.effective_status == "resolved"


def test_successful_gate_does_not_resolve_legacy_issue_without_signature(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_run = create_plan_run(repo)
    legacy_issue = write_issue_entry_for_task_id(
        paths=first_run,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Legacy integration issue without signature.",
        paths_in_scope=["src/parser.py"],
        related_tasks=["T01"],
        tags=["integration_gate", "integration_failure"],
        status="open",
    )

    second_run = create_plan_run(repo)

    def passing_verify(*, root, commands, artifact_path, cfg, timeout_s=900):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verify artifact\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    exit_code=0,
                    output="ok\n",
                    stdout="stdout line\n",
                    stderr="",
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.integration_gate.run_task_verification", passing_verify)
    passed_result = run_integration_gate(
        paths=second_run,
        cfg=AppConfig(model="test-model", verify_commands=["python -m pytest tests/unit -q"]),
        batch_index=2,
        mode="warn",
        merged_task_ids=["T01"],
        merged_paths=["src/parser.py"],
    )
    resolution, summary_path = record_integration_resolution_knowledge(
        paths=second_run, result=passed_result
    )

    assert resolution is None
    summary_text = summary_path.read_text(encoding="utf-8")
    assert legacy_issue.title in summary_text
