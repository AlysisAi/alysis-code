from __future__ import annotations

import json
from pathlib import Path

from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.knowledge_base import rebuild_knowledge_index
from alysis_code.knowledge_capture import (
    KNOWLEDGE_CAPTURE_FENCE,
    KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
    extract_knowledge_capture_block,
    persist_execution_knowledge_capture,
    promote_validated_knowledge_capture,
    validate_knowledge_capture,
)


def _valid_capture_text() -> str:
    return "\n".join(
        [
            "Implemented the parser retry fix.",
            "",
            f"```{KNOWLEDGE_CAPTURE_FENCE}",
            json.dumps(
                {
                    "schema_version": KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
                    "facts": [
                        {
                            "title": "Parser retries are bounded",
                            "summary": "Observed parser retry logic uses a bounded backoff.",
                            "paths": ["src/parser.py"],
                            "tags": ["parser", "retry"],
                        }
                    ],
                    "decisions": [
                        {
                            "decision_key": "parser-retry-backoff",
                            "title": "Keep bounded parser retry backoff",
                            "summary": "Use the bounded retry backoff for parser requests.",
                            "status": "active",
                            "paths": ["src/parser.py"],
                            "tags": ["parser", "retry"],
                        }
                    ],
                    "open_questions": ["Should retry jitter be configurable?"],
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
        ]
    )


def test_validate_knowledge_capture_accepts_bounded_payload() -> None:
    final_text = _valid_capture_text()

    block = extract_knowledge_capture_block(final_text)
    validation = validate_knowledge_capture(final_text=final_text)

    assert block is not None
    assert '"schema_version": 1' in block
    assert validation.valid is True
    assert validation.payload is not None
    assert validation.payload.facts[0].paths == ("src/parser.py",)
    assert validation.payload.decisions[0].decision_key == "parser-retry-backoff"


def test_validate_knowledge_capture_rejects_invalid_paths() -> None:
    invalid_text = "\n".join(
        [
            "Need follow-up.",
            "",
            f"```{KNOWLEDGE_CAPTURE_FENCE}",
            json.dumps(
                {
                    "schema_version": KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
                    "facts": [
                        {
                            "title": "Bad path",
                            "summary": "Should fail validation.",
                            "paths": ["/etc/passwd"],
                        }
                    ],
                    "decisions": [],
                }
            ),
            "```",
        ]
    )

    validation = validate_knowledge_capture(final_text=invalid_text)

    assert validation.valid is False
    assert validation.payload is None
    assert "invalid repo-relative path" in validation.errors[0]


def test_persist_execution_knowledge_capture_writes_artifacts_and_requires_explicit_promotion(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Capture parser knowledge", estimated_files=["src/parser.py"])
    save_plan(paths, plan)

    persisted = persist_execution_knowledge_capture(
        paths=paths,
        task=task,
        source="forge_exec",
        assistant_message=_valid_capture_text(),
        artifact_dir=paths.execution_dir / "knowledge_capture" / str(task["id"]) / "attempt_001",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    assert persisted.valid is True
    assert persisted.assistant_message_path.exists()
    assert persisted.capture_block_path is not None and persisted.capture_block_path.exists()
    assert persisted.parsed_capture_path is not None and persisted.parsed_capture_path.exists()
    assert persisted.validation_path.exists()
    assert persisted.promotion_path.exists()
    assert persisted.summary_path.exists()

    validation_payload = json.loads(persisted.validation_path.read_text(encoding="utf-8"))
    assert validation_payload["valid"] is True
    assert validation_payload["promotable_fact_count"] == 1
    assert validation_payload["promotable_decision_count"] == 1
    promotion_payload = json.loads(persisted.promotion_path.read_text(encoding="utf-8"))
    assert promotion_payload["capture_valid"] is True
    assert promotion_payload["promotion_attempted"] is False
    assert promotion_payload["promotion_succeeded"] is False
    assert promotion_payload["promotion_skipped_reason"] is None
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md")) == []
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md")) == []

    promotion = promote_validated_knowledge_capture(
        paths=paths,
        task=task,
        artifact_dir=persisted.artifact_dir,
    )

    assert len(promotion.fact_entry_ids) == 1
    assert len(promotion.decision_entry_ids) == 1

    index = rebuild_knowledge_index(paths)
    fact_entry = next(entry for entry in index.entries if entry.kind == "fact")
    decision_entry = next(entry for entry in index.entries if entry.kind == "decision")
    assert fact_entry.capture_artifact_path is not None
    assert fact_entry.capture_artifact_path.endswith("summary.md")
    assert decision_entry.decision_key == "parser-retry-backoff"
    assert decision_entry.effective_status == "active"


def test_persist_execution_knowledge_capture_is_non_fatal_for_malformed_capture(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Malformed parser capture", estimated_files=["src/parser.py"])
    save_plan(paths, plan)

    persisted = persist_execution_knowledge_capture(
        paths=paths,
        task=task,
        source="swarm_worker",
        assistant_message='Final summary only.\n\n```knowledge_capture_json\n{"schema_version": 1,}\n```',
        artifact_dir=paths.execution_dir / "knowledge_capture" / str(task["id"]) / "attempt_001",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    assert persisted.valid is False
    assert persisted.assistant_message_path.exists()
    assert persisted.capture_block_path is not None and persisted.capture_block_path.exists()
    assert persisted.parsed_capture_path is None
    validation_payload = json.loads(persisted.validation_path.read_text(encoding="utf-8"))
    assert validation_payload["valid"] is False
    summary_text = persisted.summary_path.read_text(encoding="utf-8")
    assert "## Errors" in summary_text
    assert "invalid structured knowledge JSON" in summary_text
    promotion = promote_validated_knowledge_capture(
        paths=paths,
        task=task,
        artifact_dir=persisted.artifact_dir,
    )
    assert promotion.promotion_succeeded is False
    assert (
        promotion.promotion_skipped_reason
        == "structured capture is not valid for canonical promotion"
    )
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md")) == []
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md")) == []
