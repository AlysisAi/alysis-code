from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ..agent_loop import create_session
from ..config import AppConfig, clone_cfg, load_config
from ..session_store import read_session_events
from ..surface.noop_surface import NoopSurface
from .discovery import resolve_skill_by_name
from .eval_models import (
    SkillsEvalAuthPreflightResult,
    SkillsEvalCase,
    SkillsEvalExecutionRequest,
    SkillsEvalExecutionResult,
    SkillsEvalMode,
)
from .evals import (
    default_skills_eval_output_dir,
    extract_skills_eval_metrics,
    load_skills_eval_cases,
    resolve_skills_eval_modes,
    run_skills_eval_suite,
)
from .prompting import build_explicit_skill_context_message


class OneShotSkillsEvalExecutor:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        api_key_override: str | None = None,
        session_mode: str = "auto",
    ) -> None:
        self.cfg = clone_cfg(cfg)
        self.api_key_override = api_key_override
        self.session_mode = session_mode

    def execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        session = None
        try:
            session_cfg = clone_cfg(self.cfg)
            session_cfg.skills_enabled = request.mode.skills_enabled
            session_cfg.skills_auto_invoke = request.mode.skills_auto_invoke
            session = create_session(
                cfg=session_cfg,
                root=request.workspace,
                mode=self.session_mode,
                yes=True,
                max_steps=request.max_steps,
                no_log=False,
                api_key_override=self.api_key_override,
                non_interactive=True,
                one_shot_execution=True,
                authoritative_verification_commands=(
                    [request.case.verification_command]
                    if request.case.verification_command
                    else None
                ),
                session_log_dir_override=request.sessions_dir,
                session_id_override=request.session_id,
                surface=NoopSurface(),
            )
            skills_advertised_present = _messages_contain_marker(
                session.messages,
                "<skill_context>",
            )
            repo_conventions_present = _messages_contain_marker(
                session.messages,
                "<repo_conventions>",
            )
            explicit_skill_context_used = False
            ephemeral_user_messages: list[str] | None = None
            if request.case.invocation_mode == "explicit_skill":
                explicit_skill_name = str(request.case.explicit_skill_name or "").strip()
                skill = resolve_skill_by_name(session.skill_registry, explicit_skill_name)
                if skill is None:
                    return SkillsEvalExecutionResult(
                        agent_exit_code=1,
                        skills_advertised_present=skills_advertised_present,
                        repo_conventions_present=repo_conventions_present,
                        error=f"Explicit skill not found: {explicit_skill_name}",
                    )
                explicit_skill_context_used = True
                ephemeral_user_messages = [
                    build_explicit_skill_context_message(
                        skill=skill,
                        task_text=request.case.task,
                    )
                ]

            agent_exit_code = session.run_turn(
                request.case.task,
                ephemeral_user_messages=ephemeral_user_messages,
            )
            session_log_path = session.store.path if session.store.enabled else None
            events = (
                list(read_session_events(session_log_path))
                if session_log_path is not None and session_log_path.exists()
                else []
            )
            metrics = extract_skills_eval_metrics(events, workspace_root=request.workspace)
            return SkillsEvalExecutionResult(
                agent_exit_code=agent_exit_code,
                skills_advertised_present=skills_advertised_present,
                repo_conventions_present=repo_conventions_present,
                matched_skill_context_attached=bool(metrics.get("matched_skill_context_attached")),
                matched_skill_names=tuple(metrics.get("matched_skill_names") or ()),
                explicit_skill_context_used=explicit_skill_context_used,
                skill_read_called=bool(metrics.get("skill_read_called")),
                skill_read_names=tuple(metrics.get("skill_read_names") or ()),
                skill_read_call_count=int(metrics.get("skill_read_call_count") or 0),
                skill_lifecycle_cli_used=bool(metrics.get("skill_lifecycle_cli_used")),
                skill_lifecycle_cli_commands=tuple(
                    metrics.get("skill_lifecycle_cli_commands") or ()
                ),
                skill_lifecycle_cli_call_count=int(
                    metrics.get("skill_lifecycle_cli_call_count") or 0
                ),
                manual_skill_bundle_accessed=bool(metrics.get("manual_skill_bundle_accessed")),
                manual_skill_bundle_names=tuple(metrics.get("manual_skill_bundle_names") or ()),
                manual_skill_bundle_access_count=int(
                    metrics.get("manual_skill_bundle_access_count") or 0
                ),
                tool_call_count=int(metrics.get("tool_call_count") or 0),
                completion_gate_failure_count=int(
                    metrics.get("completion_gate_failure_count") or 0
                ),
                completion_gate_incomplete_after_retries_count=int(
                    metrics.get("completion_gate_incomplete_after_retries_count") or 0
                ),
                forced_final_summary_count=int(metrics.get("forced_final_summary_count") or 0),
                verification_credit_miss_count=int(
                    metrics.get("verification_credit_miss_count") or 0
                ),
                session_log_path=session_log_path,
                session_artifact_root=(
                    session.store.session_artifact_root if session.store.enabled else None
                ),
                error=None if agent_exit_code == 0 else "agent exited non-zero",
            )
        except Exception as exc:  # noqa: BLE001
            return SkillsEvalExecutionResult(
                agent_exit_code=1,
                error=str(exc),
            )
        finally:
            if session is not None:
                session.close()


def classify_skills_eval_failure(
    error: str | None,
    *,
    agent_exit_code: int | None = None,
) -> str:
    text = str(error or "").strip().casefold()
    if not text and agent_exit_code == 0:
        return "ok"
    if any(
        marker in text
        for marker in (
            "invalid_api_key",
            "api key",
            "incorrect api key",
            "authentication",
            "unauthorized",
            "expired token",
            "invalid token",
            "permission denied",
            "permission_error",
            "401",
            "403",
        )
    ):
        return "auth"
    if any(
        marker in text
        for marker in (
            "rate limit",
            "rate_limit",
            "too many requests",
            "429",
            "tpm",
        )
    ):
        return "rate_limit"
    if any(
        marker in text
        for marker in (
            "model not found",
            "model is not set",
            "unknown model",
            "unsupported model",
            "config set model",
            "base_url",
            "connection",
            "connect",
            "timeout",
            "timed out",
            "dns",
            "proxy",
            "ssl",
            "tls",
            "refused",
            "provider",
            "bad gateway",
            "service unavailable",
            "502",
            "503",
            "504",
        )
    ):
        return "provider"
    return "runtime"


def run_skills_eval_auth_preflight(
    *,
    cfg: AppConfig,
    workspace: Path,
    output_dir: Path,
    api_key_override: str | None = None,
    session_mode: str = "auto",
    max_steps: int = 3,
    mode: SkillsEvalMode | None = None,
) -> SkillsEvalAuthPreflightResult:
    preflight_mode = mode or SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )
    preflight_case = SkillsEvalCase(
        id="auth_preflight",
        workspace=workspace,
        task="Reply with exactly OK.",
        invocation_mode="normal",
    )
    run_output_dir = output_dir.resolve()
    run_output_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = run_output_dir / "sessions"
    executor = OneShotSkillsEvalExecutor(
        cfg=cfg,
        api_key_override=api_key_override,
        session_mode=session_mode,
    )
    result = executor.execute(
        SkillsEvalExecutionRequest(
            case=preflight_case,
            mode=preflight_mode,
            workspace=workspace.resolve(),
            output_dir=run_output_dir,
            sessions_dir=sessions_dir,
            session_id="skills_eval_auth_preflight",
            max_steps=max(1, int(max_steps)),
        )
    )
    classification = classify_skills_eval_failure(
        result.error,
        agent_exit_code=result.agent_exit_code,
    )
    ok = classification == "ok"
    message = "Auth preflight passed." if ok else str(result.error or "auth preflight failed")
    return SkillsEvalAuthPreflightResult(
        ok=ok,
        classification=classification,  # type: ignore[arg-type]
        message=message,
        agent_exit_code=result.agent_exit_code,
        session_log_path=result.session_log_path,
        session_artifact_root=result.session_artifact_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Skills evaluation harness for Alysis Code.",
    )
    parser.add_argument(
        "--manifest", required=True, help="Path to the skills eval cases JSON manifest."
    )
    parser.add_argument(
        "--output-dir",
        help="Output directory for eval artifacts. Defaults to ./.alysis/evals/skills/<timestamp>/",
    )
    parser.add_argument(
        "--mode",
        action="append",
        dest="modes",
        help="Restrict execution to one or more eval modes.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Restrict execution to one or more case ids.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=25,
        help="Per-run max_steps passed into the existing one-shot runtime.",
    )
    parser.add_argument("--model", help="Optional model override for this eval run.")
    parser.add_argument("--api-key", help="Optional API key override for this eval run.")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).expanduser().resolve()
    cases = load_skills_eval_cases(manifest_path)
    if args.case_ids:
        wanted = {str(item).strip().casefold() for item in args.case_ids if str(item).strip()}
        cases = tuple(case for case in cases if case.id.casefold() in wanted)
    modes = resolve_skills_eval_modes(args.modes)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else default_skills_eval_output_dir(root=Path.cwd())
    )

    cfg = load_config()
    if args.model:
        cfg.model = str(args.model).strip()

    executor = OneShotSkillsEvalExecutor(
        cfg=cfg,
        api_key_override=args.api_key,
    )
    artifacts = run_skills_eval_suite(
        cases=cases,
        modes=modes,
        output_dir=output_dir,
        executor=executor,
        manifest_path=manifest_path,
        max_steps=max(1, int(args.max_steps)),
    )

    print(f"Skills eval output: {artifacts.output_dir}")
    print(f"Raw results: {artifacts.results_path}")
    print(f"Summary JSON: {artifacts.summary_json_path}")
    print(f"Summary Markdown: {artifacts.summary_md_path}")
    modes_obj = artifacts.summary.get("modes")
    if isinstance(modes_obj, dict):
        for mode_name in sorted(modes_obj):
            mode_payload = modes_obj.get(mode_name)
            if not isinstance(mode_payload, dict):
                continue
            executed = mode_payload.get("executed_runs", 0)
            pass_rate = mode_payload.get("pass_rate")
            print(
                f"- {mode_name}: executed={executed}, pass_rate={_format_rate_for_cli(pass_rate)}"
            )
    return 0


def _messages_contain_marker(messages: Sequence[dict[str, object]], marker: str) -> bool:
    return any(marker in str(message.get("content") or "") for message in messages)


def _format_rate_for_cli(value: object) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{numeric * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
