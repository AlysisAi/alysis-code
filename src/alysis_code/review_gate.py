from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    AppConfig,
    ConfigError,
    resolve_llm_timeout_s,
    resolve_model_access_api_key,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
    resolve_role_temperature,
)
from .diff_paths import parse_patch_changed_files as _parse_patch_changed_files
from .execution_shared import safe_task_file_component
from .forge import RunPaths, ensure_execution_dirs, now_iso
from .llm.base import ChatClient
from .llm.factory import _resolve_base_url, make_llm_client
from .llm.openai_compat import OpenAICompatClient as _OpenAICompatClient
from .llm.types import LLMError
from .model_metadata_policy import (
    ActiveModelRef,
    ModelMetadataPolicyError,
    evaluate_active_model_metadata_policy,
)
from .model_registry import ModelRegistry
from .model_router import ROLE_REVIEW, resolve_model_for_role
from .profiles import get_active_profile
from .verify_gate import compact_verification_payload

_CONFIDENCE = {"high", "medium", "low"}
_AC_STATUS = {"met", "unclear", "not_met"}

# Compatibility surface for tests and downstream monkeypatches that predate the LLM factory.
OpenAICompatClient = _OpenAICompatClient

REVIEWER_SYSTEM_PROMPT = """You are REVIEWER - a strict senior code reviewer for forge task patches.

Primary goal
- Decide whether the submitted patch should be accepted as done for the given task.
- Be conservative: if anything important is unclear, unverified, or out-of-scope, do NOT approve.

You will be given
- Task details: description, acceptance criteria, estimated files, write scope.
- A list of changed files.
- A patch excerpt (may be truncated).
- A worker report excerpt and verification evidence when available.

Review rubric (apply in order; definition of done)
1) Acceptance criteria: For each criterion, decide met / unclear / not_met and cite evidence from the patch/report.
2) Scope and safety:
   - Changes must stay within write_scope.
   - No forbidden paths (e.g., .alysis/, secrets, unexpected generated artifacts).
   - No secret leakage (tokens, API keys) in logs/output/comments.
3) Correctness:
   - Fixes the root cause, not just symptoms.
   - Handles edge cases reasonably.
   - No placeholder code or TODO implement unless explicitly intended.
4) Tests:
   - Bug fix -> regression test.
   - New behavior -> appropriate unit/integration tests (following repo patterns).
   - Commands/results should be reported or the lack of running tests should be justified.
5) Docs:
   - If CLI/config/output/API behavior changes -> README.md and/or docs/ updated.
6) Maintainability:
   - Minimal diffs, consistent style, clear naming, avoids unrelated refactors.

Output contract
- Return STRICT JSON only. No markdown, no extra text.
- Confidence should reflect evidence strength.
- For every issue, provide a concrete suggested fix.
"""


class ReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewOutcome:
    task_id: str
    approved: bool
    confidence: str
    summary: str
    blocking_issues_count: int
    non_blocking_issues_count: int
    json_path: Path
    markdown_path: Path


def _truncate_head_tail(text: str, *, max_chars: int = 120_000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return text[:half] + f"\n\n... [truncated {omitted} characters] ...\n\n" + text[-half:]


def _read_text_best_effort(path: Path | None) -> str:
    if path is None or not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_patch_changed_files(patch_text: str) -> list[str]:
    return _parse_patch_changed_files(patch_text)


def _resolve_artifact_path(root: Path, artifact_ref: str | None) -> Path | None:
    candidate = str(artifact_ref or "").strip()
    if not candidate:
        return None
    path = Path(candidate)
    if path.is_absolute():
        return path
    return root / path


def _verification_context_from_sources(
    *,
    paths: RunPaths,
    verification_payload_override: dict[str, Any] | None,
    raw_worker: dict[str, Any] | None,
    verify_artifact_path: Path,
    report_excerpt: str,
) -> dict[str, Any] | None:
    override_context = compact_verification_payload(verification_payload_override)
    if override_context is not None:
        override_context["evidence_source"] = "override"
        return override_context

    if isinstance(raw_worker, dict):
        worker_payload = compact_verification_payload(raw_worker.get("verify_payload"))
        if worker_payload is not None:
            worker_payload["evidence_source"] = "worker_result"
            return worker_payload

    artifact_candidate = verify_artifact_path
    verify_summary = ""
    verify_failed: bool | None = None
    if isinstance(raw_worker, dict):
        artifact_candidate = (
            _resolve_artifact_path(
                paths.root,
                str(raw_worker.get("verify_artifact_path") or "").strip() or None,
            )
            or verify_artifact_path
        )
        verify_summary = str(raw_worker.get("verify_summary") or "").strip()
        raw_verify_failed = raw_worker.get("verify_failed")
        if isinstance(raw_verify_failed, bool):
            verify_failed = raw_verify_failed
    artifact_excerpt = _truncate_head_tail(
        _read_text_best_effort(artifact_candidate),
        max_chars=8_000,
    )
    if artifact_excerpt:
        artifact_context: dict[str, Any] = {
            "evidence_source": "verify_artifact",
            "artifact_excerpt": artifact_excerpt,
        }
        if verify_summary:
            artifact_context["summary"] = verify_summary
        if verify_failed is not None:
            artifact_context["all_passed"] = not verify_failed
        try:
            artifact_context["artifact_path"] = (
                artifact_candidate.resolve().relative_to(paths.root.resolve()).as_posix()
            )
        except ValueError:
            artifact_context["artifact_path"] = str(artifact_candidate)
        return artifact_context

    if report_excerpt:
        report_context: dict[str, Any] = {
            "evidence_source": "report_excerpt",
        }
        if verify_summary:
            report_context["summary"] = verify_summary
        return report_context
    return None


def _extract_json_block(text: str) -> dict[str, Any]:
    raw = text.strip()
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.DOTALL)
    if fenced:
        try:
            value = json.loads(fenced.group(1))
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError as e:
            raise ReviewError(f"Invalid JSON in fenced block: {e}") from e

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            value = json.loads(raw[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    raise ReviewError("Review model did not return valid JSON.")


def _make_review_llm_client(
    *,
    cfg: AppConfig,
    api_key: str,
    model_name: str,
) -> ChatClient:
    temperature = resolve_role_temperature(cfg, role="review")
    if OpenAICompatClient is _OpenAICompatClient:
        return make_llm_client(
            cfg=cfg,
            api_key=api_key,
            model=model_name,
            timeout_s=resolve_llm_timeout_s(cfg),
            temperature=temperature,
            prompt_cache_key=resolve_prompt_cache_key(cfg),
            prompt_cache_retention=resolve_prompt_cache_retention(cfg),
        )

    profile = get_active_profile(cfg)
    return OpenAICompatClient(
        base_url=_resolve_base_url(cfg=cfg, profile=profile),
        api_key=api_key,
        model=model_name,
        timeout_s=resolve_llm_timeout_s(cfg),
        temperature=temperature,
        prompt_cache_key=resolve_prompt_cache_key(cfg),
        prompt_cache_retention=resolve_prompt_cache_retention(cfg),
        extra_headers=profile.extra_headers,
    )


def _normalize_issue_list(value: Any, *, key: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ReviewError(f"Review JSON field '{key}' must be a list.")
    out: list[dict[str, str]] = []
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ReviewError(f"Review JSON field '{key}[{idx}]' must be an object.")
        title = str(item.get("title") or "").strip()
        details = str(item.get("details") or "").strip()
        suggested_fix = str(item.get("suggested_fix") or "").strip()
        out.append(
            {
                "title": title or "(missing title)",
                "details": details or "(missing details)",
                "suggested_fix": suggested_fix or "(missing suggested_fix)",
            }
        )
    return out


def _normalize_ac_checks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ReviewError("Review JSON field 'acceptance_criteria_checks' must be a list.")
    out: list[dict[str, str]] = []
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ReviewError(
                f"Review JSON field 'acceptance_criteria_checks[{idx}]' must be an object."
            )
        criterion = str(item.get("criterion") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        evidence = str(item.get("evidence") or "").strip()
        if status not in _AC_STATUS:
            raise ReviewError(
                f"Review JSON field 'acceptance_criteria_checks[{idx}].status' "
                "must be one of: met, unclear, not_met."
            )
        out.append(
            {
                "criterion": criterion or "(missing criterion)",
                "status": status,
                "evidence": evidence or "(missing evidence)",
            }
        )
    return out


def normalize_review_json(raw: dict[str, Any]) -> dict[str, Any]:
    approved = raw.get("approved")
    if not isinstance(approved, bool):
        raise ReviewError("Review JSON field 'approved' must be a boolean.")

    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence not in _CONFIDENCE:
        raise ReviewError("Review JSON field 'confidence' must be high|medium|low.")

    summary = str(raw.get("summary") or "").strip()
    if not summary:
        raise ReviewError("Review JSON field 'summary' is required.")

    blocking = _normalize_issue_list(raw.get("blocking_issues"), key="blocking_issues")
    non_blocking = _normalize_issue_list(raw.get("non_blocking_issues"), key="non_blocking_issues")
    ac_checks = _normalize_ac_checks(raw.get("acceptance_criteria_checks"))

    return {
        "approved": approved,
        "confidence": confidence,
        "blocking_issues": blocking,
        "non_blocking_issues": non_blocking,
        "acceptance_criteria_checks": ac_checks,
        "summary": summary,
    }


def _enforce_conservative_approval(
    *,
    task: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    if not bool(review.get("approved")):
        return review

    reasons: list[str] = []
    blocking = list(review.get("blocking_issues") or [])
    ac_checks = list(review.get("acceptance_criteria_checks") or [])

    if blocking:
        reasons.append("blocking issues were reported")
    if any(str(item.get("status") or "").lower() != "met" for item in ac_checks):
        reasons.append("some acceptance criteria are unclear or not met")

    task_ac_raw = task.get("acceptance_criteria")
    task_ac = []
    if isinstance(task_ac_raw, list):
        task_ac = [str(item).strip() for item in task_ac_raw if str(item).strip()]
    if task_ac and not ac_checks:
        reasons.append("acceptance criteria checks are missing")

    if not reasons:
        return review

    enforced = dict(review)
    enforced["approved"] = False
    enforced_blocking = list(blocking)
    enforced_blocking.append(
        {
            "title": "Approval policy violation",
            "details": (
                "Review was marked approved, but "
                + "; ".join(reasons)
                + ". Conservative policy requires non-approval in this case."
            ),
            "suggested_fix": (
                "Set approved=false until all acceptance criteria are met and no blocking issues "
                "remain."
            ),
        }
    )
    enforced["blocking_issues"] = enforced_blocking
    return enforced


def _review_markdown(
    *,
    task: dict[str, Any],
    review: dict[str, Any],
    changed_files: list[str],
) -> str:
    task_id = str(task.get("id") or "")
    task_title = str(task.get("title") or "")
    lines: list[str] = [
        f"# Review: {task_id}",
        "",
        f"- Title: {task_title}",
        f"- Approved: {'yes' if review['approved'] else 'no'}",
        f"- Confidence: {review['confidence']}",
        f"- Generated At: {now_iso()}",
        "",
        "## Summary",
        "",
        review["summary"],
        "",
        "## Acceptance Criteria Checks",
        "",
    ]
    if review["acceptance_criteria_checks"]:
        for check in review["acceptance_criteria_checks"]:
            lines.append(f"- `{check['status']}` {check['criterion']} - {check['evidence']}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Blocking Issues", ""])
    if review["blocking_issues"]:
        for issue in review["blocking_issues"]:
            lines.append(f"- **{issue['title']}**: {issue['details']}")
            lines.append(f"  - Suggested fix: {issue['suggested_fix']}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Non-blocking Issues", ""])
    if review["non_blocking_issues"]:
        for issue in review["non_blocking_issues"]:
            lines.append(f"- **{issue['title']}**: {issue['details']}")
            lines.append(f"  - Suggested fix: {issue['suggested_fix']}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Changed Files", ""])
    if changed_files:
        for path in changed_files:
            lines.append(f"- `{path}`")
    else:
        lines.append("- (none detected)")
    return "\n".join(lines).rstrip() + "\n"


def _build_review_prompt(
    *,
    task: dict[str, Any],
    changed_files: list[str],
    patch_excerpt: str,
    report_excerpt: str,
    worker_context: dict[str, Any],
    verification_context: dict[str, Any] | None,
) -> str:
    task_payload = {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "write_scope": task.get("write_scope", []),
        "estimated_files": task.get("estimated_files", []),
        "branch": task.get("branch", ""),
        "status": task.get("status", ""),
    }
    payload = {
        "task": task_payload,
        "changed_files": changed_files,
        "verification": verification_context,
        "worker_context": worker_context,
        "report_excerpt": report_excerpt,
        "patch_excerpt": patch_excerpt,
    }
    schema_hint = {
        "approved": "bool",
        "confidence": "high|medium|low",
        "summary": "str",
        "acceptance_criteria_checks": [
            {"criterion": "str", "status": "met|unclear|not_met", "evidence": "str"}
        ],
        "blocking_issues": [{"title": "str", "details": "str", "suggested_fix": "str"}],
        "non_blocking_issues": [{"title": "str", "details": "str", "suggested_fix": "str"}],
    }
    return (
        "Review this task patch and produce STRICT JSON only.\n\n"
        "Review checklist:\n"
        "- Correctness: does the patch fully address the problem? Include edge cases.\n"
        "- Tests: were tests added/updated where needed? Are commands/results addressed?\n"
        "- Docs: if behavior/CLI/usage changed, were README/docs/help updates included?\n"
        "- Safety: secrets, injection risks, unsafe shell usage, path traversal, etc.\n"
        "- Maintainability: naming, structure, comments, consistency, minimal diff.\n"
        "- Definition of done must be enforced conservatively. If unclear, do NOT approve.\n\n"
        "Input context:\n"
        f"{json.dumps(payload, ensure_ascii=True, indent=2)}\n\n"
        "Required JSON schema:\n"
        f"{json.dumps(schema_hint, ensure_ascii=True, indent=2)}\n"
    )


def review_task(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    task: dict[str, Any],
    cfg: AppConfig,
    api_key_override: str | None,
    verification_payload_override: dict[str, Any] | None = None,
) -> ReviewOutcome:
    ensure_execution_dirs(paths)
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise ReviewError("Task id is empty.")

    safe = safe_task_file_component(task_id)
    patch_path = paths.execution_patches_dir / f"{safe}.diff"
    report_path = paths.execution_reports_dir / f"{safe}.md"
    worker_result_path = paths.execution_dir / "worker_results" / f"{safe}.json"
    review_json_path = paths.execution_reviews_dir / f"{safe}.json"
    review_md_path = paths.execution_reviews_dir / f"{safe}.md"

    if not patch_path.exists():
        raise ReviewError(f"Missing patch artifact: {patch_path}")

    patch_text = _read_text_best_effort(patch_path)
    patch_excerpt = _truncate_head_tail(patch_text)
    report_excerpt = _truncate_head_tail(_read_text_best_effort(report_path), max_chars=30_000)
    changed_files = parse_patch_changed_files(patch_text)
    worker_context: dict[str, Any] = {}
    raw_worker: dict[str, Any] | None = None

    if worker_result_path.exists():
        try:
            raw_worker = json.loads(worker_result_path.read_text(encoding="utf-8"))
            if isinstance(raw_worker, dict):
                from_worker = raw_worker.get("changed_files")
                if isinstance(from_worker, list):
                    worker_files = [str(x).strip() for x in from_worker if str(x).strip()]
                    if worker_files:
                        changed_files = worker_files
                verify_summary = str(raw_worker.get("verify_summary") or "").strip()
                if verify_summary:
                    worker_context["verify_summary"] = verify_summary
                worker_context["verify_failed"] = bool(raw_worker.get("verify_failed"))
            warnings_raw = raw_worker.get("warnings")
            if isinstance(warnings_raw, list):
                warning_items = [str(item).strip() for item in warnings_raw if str(item).strip()]
                if warning_items:
                    worker_context["warnings"] = warning_items[:10]
        except (OSError, json.JSONDecodeError):
            pass
    verification_context = _verification_context_from_sources(
        paths=paths,
        verification_payload_override=verification_payload_override,
        raw_worker=raw_worker,
        verify_artifact_path=paths.execution_verify_dir / f"{safe}.txt",
        report_excerpt=report_excerpt,
    )

    prompt = _build_review_prompt(
        task=task,
        changed_files=changed_files,
        patch_excerpt=patch_excerpt,
        report_excerpt=report_excerpt,
        worker_context=worker_context,
        verification_context=verification_context,
    )

    try:
        api_key = resolve_model_access_api_key(cfg, override=api_key_override)
    except ConfigError as e:
        raise ReviewError(str(e)) from e
    model_name = resolve_model_for_role(
        cfg=cfg,
        role=ROLE_REVIEW,
        plan=plan,
        prefer_context="forge",
    )
    registry = ModelRegistry(cfg=cfg, api_key=api_key)
    try:
        metadata_policy_result = evaluate_active_model_metadata_policy(
            cfg=cfg,
            registry=registry,
            active_models=[ActiveModelRef(role=ROLE_REVIEW, model_name=model_name)],
        )
    except ModelMetadataPolicyError as e:
        raise ReviewError(str(e)) from e
    for warning_message in metadata_policy_result.warning_messages:
        warnings.warn(warning_message, stacklevel=2)

    client = _make_review_llm_client(
        cfg=cfg,
        api_key=api_key,
        model_name=model_name,
    )
    try:
        response = client.chat(
            messages=[
                {
                    "role": "system",
                    "content": REVIEWER_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
    except LLMError as e:
        raise ReviewError(f"Review LLM request failed: {e}") from e

    normalized = normalize_review_json(_extract_json_block(response.content))
    normalized = _enforce_conservative_approval(task=task, review=normalized)
    normalized["task_id"] = task_id
    normalized["task_title"] = str(task.get("title") or "")
    normalized["generated_at"] = now_iso()
    normalized["changed_files"] = changed_files

    review_json_path.write_text(
        json.dumps(normalized, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    review_md_path.write_text(
        _review_markdown(task=task, review=normalized, changed_files=changed_files),
        encoding="utf-8",
    )

    return ReviewOutcome(
        task_id=task_id,
        approved=bool(normalized["approved"]),
        confidence=str(normalized["confidence"]),
        summary=str(normalized["summary"]),
        blocking_issues_count=len(normalized["blocking_issues"]),
        non_blocking_issues_count=len(normalized["non_blocking_issues"]),
        json_path=review_json_path,
        markdown_path=review_md_path,
    )
