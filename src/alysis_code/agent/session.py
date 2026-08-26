from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import subprocess
import threading
import warnings
from collections import Counter
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..agent import _patchable
from ..agentbox_integration import AgentBoxTelemetry
from ..background_runner import (
    DisabledBackgroundRunner,
    LazyBackgroundShellRunner,
    build_background_shell_runner_from_settings,
)
from ..branding import env_get
from ..budget_policy import (
    BUDGET_STOP_EXIT_CODE,
    STOP_REASON_RUN_BUDGET_EXHAUSTED,
    is_budget_cancellation,
    resolve_budget_grace_seconds,
)
from ..build_identity import load_build_info
from ..cancellation import CooperativeCancellationError, EventCancellationToken
from ..compaction.conversation_compactor import ConversationCompactor
from ..compaction.settings import resolve_compaction_settings
from ..compaction.tool_output_offload import ToolOutputOffloader
from ..config import (
    AppConfig,
    ConfigError,
    get_api_key,
    resolve_api_key,
    resolve_crash_diagnostic_log_path,
    resolve_llm_enable_thinking,
    resolve_llm_reasoning_effort,
    resolve_llm_timeout_s,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
    resolve_role_temperature,
)
from ..crash_diagnostics import (
    CrashDiagnosticLogger,
    build_crash_diagnostic_logger,
    build_error_event_fields,
)
from ..custom_tools import CustomToolSessionState, build_custom_tool_session_state
from ..durable_service_manager import DurableServiceManager
from ..edit_discipline import EditDisciplineState
from ..error_text import sanitize_error_text_for_output
from ..execution_deadline import (
    MINIMUM_FORCED_SUMMARY_SECONDS,
    DeadlineExhausted,
    ExecutionDeadline,
    temporarily_clamp_client_timeout,
)
from ..extensions.activation import (
    ActivationDecision,
    WorkspaceTrustPromptFn,
    WorkspaceTrustPromptRequest,
)
from ..hooks import (
    HOOK_AUDIT_ARTIFACT_PARTS,
    HookDispatcher,
    HookDispatchResult,
    ResolvedHookConfig,
    load_resolved_hooks_config,
)
from ..host_actions import HostActionHandler
from ..internal_artifacts import (
    ArtifactVisibility,
    mark_message_internal,
    summary_input_messages,
)
from ..llm.base import (
    ChatClient,
    count_input_tokens_if_supported,
    effective_tools_for_client,
)
from ..llm.cache_policy import build_prompt_cache_namespace, derive_prompt_cache_stream_key
from ..llm.factory import _resolve_base_url, make_llm_client
from ..llm.metadata import (
    PROVIDER_METADATA_KEY,
    assistant_message_from_response,
    endpoint_descriptor,
)
from ..llm.openai_compat import OpenAICompatClient as _OpenAICompatClient
from ..llm.protocols import OPENAI_COMPAT_PROTOCOL, get_provider_protocol_capabilities
from ..llm.types import UsageConfidence, UsageSource
from ..mcp.config import load_resolved_mcp_config
from ..mcp.manager import ForgeTaskScopedMcpManager, McpManager, create_mcp_manager
from ..model_metadata_policy import ActiveModelRef, evaluate_active_model_metadata_policy
from ..model_registry import ModelRegistry, resolve_model_provider_key
from ..model_router import ROLE_CODING, ROLE_COMPACTOR, resolve_model_for_role
from ..personas import (
    PersonaSwitchState,
    load_custom_personas,
    normalize_persona,
    persona_modes_enabled,
)
from ..process_reaping import (
    ProcessGroupRegistry,
    ReapAction,
    ReapEvent,
    _process_reaping_enabled,
    reap_tracked_groups,
    resolve_reap_decision,
    survivor_payloads,
)
from ..profile_presets import find_preset_for_profile
from ..profiles import get_active_profile, resolve_effective_base_url
from ..provider_telemetry import (
    ProviderCallTelemetryRecorder,
    provider_telemetry_operation,
    provider_telemetry_record_count,
    set_provider_telemetry_sink,
)
from ..repo_scan import scan_workspace as scan_workspace
from ..request_estimation import (
    estimate_request_token_breakdown,
    estimate_request_tokens,
    request_contains_media,
    request_message_signatures,
    tool_schema_signature,
)
from ..run_provenance import (
    CONFIG_SNAPSHOT_EVENT,
    config_snapshot_payload,
    resolve_sampling_settings,
    set_active_sampling_settings,
)
from ..runtime_context_features import resolve_runtime_context_features
from ..runtime_kind import RuntimeKind, resolve_session_runtime_kind
from ..sandbox_runner import (
    DisabledShellRunner,
    LazyShellRunner,
    build_shell_runner,
    build_shell_runner_from_settings,
    with_closed_stdin,
)
from ..sandbox_settings import resolve_shell_sandbox_settings
from ..service_persistence import PersistentServiceRegistry
from ..session_store import SessionStore, make_session_id, resolve_sessions_dir
from ..skills import ConventionDocument, SkillBundle, SkillCatalogEntry
from ..step_budget import StepBudgetRuntime, normalize_step_budget_policy
from ..subagents import SubagentDefinition, unavailable_builtin_subagents
from ..surface import ApprovalRequest, NoopSurface, StatusEvent
from ..surface.base import Surface
from ..terminal_manager import TerminalManager
from ..tools.registry import iter_builtin_tool_metadata
from ..usage_tracker import (
    ContextLeft,
    RequestContextMeasurement,
    UsageSummary,
    build_usage_record,
    compute_context_left,
    usage_context_from_client_response,
)
from ..verify_gate import (
    ResolvedVerifyCommands,
    is_authoritative_verify_command_selection,
    verification_selection_payload,
)
from ..workspace_binding import WorkspaceBinding
from ..workspace_provisioning import (
    ProvisioningAction,
    ShellCommandRunner,
    ShellProbeResult,
    _workspace_provisioning_enabled,
    detect_declared_test_runner,
    probe_runner_importable,
    provision_test_runner,
    provisioning_already_attempted,
    resolve_provisioning_decision,
)
from .cache_keepalive import (
    CacheKeepaliveRequest,
    ParentCacheKeepalive,
    cache_keepalive_unsupported_reason,
)
from .empty_response_stall import EmptyResponseStallTracker
from .errors import SessionWorkdirError
from .llm_calls import _main_agent_chat, _rewrite_final_summary_for_language
from .prompt_context import (
    _build_plugin_activation_index,
    _component_plugin_allowed,
    _merge_dropped_counts,
    _normalize_workspace_relpath,
    _PluginActivationIndex,
    _repo_summary_data,
    _resolve_requested_workdir_within_workspace,
    _session_verify_command_selection,
    _subagent_context_message,
    _workspace_binding_context_message,
    _workspace_relpath_for_path,
    _WorkspaceGroundingDescriptor,
    prepare_session_prompt_context,
    resolve_session_active_workdir_path,
    resolve_session_active_workdir_relpath,
    resolve_workdir_relpath_within_workspace,
    set_session_active_workdir,
)
from .read_ledger import SessionReadLedger
from .tools_assembly import (
    ToolDef,
    ToolDispatchGuard,
    _custom_tools_write_scope_restricted,
    _filter_custom_tool_session_state_for_plugins,
    _filter_mcp_config_for_plugins,
    build_tools,
)
from .turn import (
    _FORCED_FINAL_SUMMARY_SYSTEM_PROMPT_TEMPLATE,
    _looks_like_unexecuted_tool_call_markup,
)
from .turn import run_turn as _run_turn
from .turn.events import (
    _emit_assistant_message_events,
    _legacy_message_tool_events_required,
)

if TYPE_CHECKING:
    from ..ide.managed_browser import ManagedBrowserService
from .steering import SteerInbox
from .subagent_execution import ChildScheduler

OpenAICompatClient = _OpenAICompatClient
_DEFAULT_CREATE_MCP_MANAGER = create_mcp_manager


def _build_workspace_trust_prompt(
    *,
    surface: Surface,
    non_interactive: bool,
) -> WorkspaceTrustPromptFn | None:
    if non_interactive or env_get("ALYSIS_CI") == "1":
        return None

    def prompt(request: WorkspaceTrustPromptRequest) -> bool:
        preview = (
            f"Workspace: {request.repo_root}\n"
            f"Overrides SHA-256: {request.overrides_sha256}\n"
            f"Project enables: {', '.join(request.plugins_added) or '-'}\n"
            f"Project disables: {', '.join(request.plugins_removed) or '-'}"
        )
        decision = surface.request_approval(
            ApprovalRequest(
                kind="workspace_trust",
                reason="Trust this workspace's plugin enable/disable overrides?",
                preview=preview,
                files=[request.repo_root],
                metadata=request.model_dump(mode="json"),
            )
        )
        return bool(decision.allow)

    return prompt


def _hook_plugin_id(hook_id: str | None, index: _PluginActivationIndex) -> str | None:
    raw = str(hook_id or "").strip()
    if "." not in raw:
        return None
    return index.slug_to_plugin_id.get(raw.split(".", 1)[0])


def _filter_hooks_config_for_plugins(
    *,
    config: ResolvedHookConfig,
    activation_decision: ActivationDecision,
    index: _PluginActivationIndex,
) -> tuple[ResolvedHookConfig, Counter[str]]:
    dropped_counts: Counter[str] = Counter()
    groups_by_event: dict[str, tuple[Any, ...]] = {}
    for event_name, groups in config.groups_by_event.items():
        kept_groups = []
        for group in groups:
            kept_hooks = tuple(
                hook
                for hook in group.hooks
                if _component_plugin_allowed(
                    _hook_plugin_id(hook.id, index),
                    activation_decision,
                    dropped_counts,
                )
            )
            if kept_hooks:
                kept_groups.append(replace(group, hooks=kept_hooks))
        groups_by_event[event_name] = tuple(kept_groups)
    return (
        ResolvedHookConfig(
            groups_by_event=groups_by_event,
            loaded_paths=config.loaded_paths,
            untrusted_project_paths=config.untrusted_project_paths,
        ),
        dropped_counts,
    )


def _make_session_llm_client(
    *,
    cfg: AppConfig,
    api_key: str,
    model: str,
    timeout_s: float | None,
    temperature: float,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
    prompt_cache_namespace: str | None,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
    session_id: str | None,
) -> ChatClient:
    openai_client_cls = _patchable("OpenAICompatClient", OpenAICompatClient)
    if openai_client_cls is _OpenAICompatClient:
        return make_llm_client(
            cfg=cfg,
            api_key=api_key,
            model=model,
            timeout_s=timeout_s,
            temperature=temperature,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
            prompt_cache_namespace=prompt_cache_namespace,
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
            session_id=session_id,
        )

    profile = get_active_profile(cfg)
    return openai_client_cls(
        base_url=_resolve_base_url(cfg=cfg, profile=profile),
        api_key=api_key,
        model=model,
        timeout_s=60.0 if timeout_s is None else timeout_s,
        temperature=temperature,
        prompt_cache_key=prompt_cache_key,
        prompt_cache_retention=prompt_cache_retention,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        extra_headers=profile.extra_headers,
    )


def _git_branch(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", os.fspath(root), "rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "-"
    if proc.returncode != 0:
        return "-"
    branch = proc.stdout.strip()
    return branch or "-"


def _git_is_dirty(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", os.fspath(root), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def _surface_needs_startup_git_status(surface: Surface) -> bool:
    if isinstance(surface, NoopSurface):
        return False
    # RichSurface stores this flag internally; when hidden, skip expensive startup git probes.
    show_status_line = getattr(surface, "_show_status_line", None)
    if isinstance(show_status_line, bool):
        return show_status_line
    return True


def _meaningful_surface_warning_handler(surface: Surface | object) -> Callable[[str], None] | None:
    surface_cls = getattr(surface, "__class__", None)
    handler = getattr(surface, "emit_warning", None)
    if callable(handler):
        cls_handler = getattr(surface_cls, "emit_warning", None)
        if cls_handler is not getattr(NoopSurface, "emit_warning", None):
            return handler
    return _meaningful_surface_legacy_warning_handler(surface)


def _meaningful_surface_legacy_warning_handler(
    surface: Surface | object,
) -> Callable[[str], None] | None:
    surface_cls = getattr(surface, "__class__", None)
    handler = getattr(surface, "on_warning", None)
    if not callable(handler):
        return None
    cls_handler = getattr(surface_cls, "on_warning", None)
    if cls_handler is getattr(NoopSurface, "on_warning", None):
        return None
    return handler


def _repo_summary(root: Path) -> str:
    return _repo_summary_data(root).text


def _disable_unsupported_native_streaming(
    *,
    cfg: AppConfig,
) -> tuple[AppConfig, str | None]:
    if not bool(getattr(cfg, "stream", False)):
        return cfg, None
    profile = get_active_profile(cfg)
    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    if protocol == OPENAI_COMPAT_PROTOCOL:
        return cfg, None
    base_url = _resolve_base_url(cfg=cfg, profile=profile)
    provider_key = resolve_model_provider_key(
        cfg=cfg,
        model_name=cfg.model,
        base_url=base_url,
        profile_name=profile.name,
    )
    capabilities = get_provider_protocol_capabilities(
        provider_key=provider_key,
        protocol=protocol,
    )
    # Unknown provider capabilities must not disable streaming: assume it works
    # and rely on the per-step stream-unsupported fallback in the turn loop to
    # downgrade at runtime if the provider rejects it.
    streaming_supported = capabilities.supports_streaming if capabilities is not None else True
    if streaming_supported:
        return cfg, None
    warning = (
        f"Streaming requested but profile {profile.name!r} uses protocol={protocol!r}, "
        "which does not support streaming yet in Alysis Code; streaming is disabled for this run."
    )
    return cfg.model_copy(update={"stream": False}, deep=True), warning


class ForcedFinalSummaryTerminationKind(StrEnum):
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    COMPLETION_GATE_STAGNATION = "completion_gate_stagnation"
    EXECUTION_GUARD_STAGNATION = "execution_guard_stagnation"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    OTHER = "other"


def _normalize_forced_summary_termination_kind(
    value: str | ForcedFinalSummaryTerminationKind,
) -> ForcedFinalSummaryTerminationKind:
    if isinstance(value, ForcedFinalSummaryTerminationKind):
        return value
    normalized = str(value or "").strip().lower()
    for item in ForcedFinalSummaryTerminationKind:
        if normalized == item.value:
            return item
    return ForcedFinalSummaryTerminationKind.OTHER


def _add_event_diagnostics(
    payload: dict[str, Any],
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    if not diagnostics:
        return payload
    for key, value in diagnostics.items():
        payload.setdefault(key, value)
    return payload


@dataclass
class AgentSession:
    cfg: AppConfig
    root: Path
    mode: str
    yes: bool
    stream: bool
    routing_mode: str
    max_steps: int | None
    console: Any | None
    surface: Surface
    store: SessionStore
    client: ChatClient
    model_registry: ModelRegistry
    usage_summary: UsageSummary
    usage_role: str
    tool_output_offloader: ToolOutputOffloader | None
    conversation_compactor: ConversationCompactor | None
    tool_output_offload_enabled: bool
    conversation_summarization_enabled: bool
    compaction_profile: str
    tools: dict[str, ToolDef]
    tool_list: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    _cache_efficiency_summary_recorded: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    # Machine-readable reason this run stopped, set by whichever path
    # terminates the turn. Read by close() so the run_finished crash event
    # carries it, which is how a harness tells a budget stop from a crash.
    stop_reason: str | None = field(default=None, init=False, repr=False)
    startup_messages: list[dict[str, Any]] = field(default_factory=list)
    runtime_kind: RuntimeKind = RuntimeKind.INTERACTIVE_CHAT
    prompt_cache_stream_key: str | None = None
    mcp_manager: McpManager | ForgeTaskScopedMcpManager | None = None
    terminal_manager: TerminalManager | None = None
    durable_service_manager: DurableServiceManager | None = None
    persistent_service_registry: PersistentServiceRegistry | None = None
    edit_discipline: EditDisciplineState | None = None
    managed_browser_service: ManagedBrowserService | None = None
    managed_browser_owner_id: str | None = None
    managed_browser_cancel_check: Callable[[], bool] | None = None
    router_client: Any | None = None
    _semantic_router_bound_client: Any | None = None
    _provisioned_router_client: Any | None = None
    api_key: str = ""
    api_key_source: str = "missing"
    shell_runner: Any | None = None
    no_log: bool = False
    non_interactive: bool = False
    one_shot_execution: bool = False
    # Persona mode (code|architect|ask|debug). A convention layered on the
    # execution-mode gate, never an enforcement layer; "code" is the no-op
    # persona. See docs/persona_modes_design.md.
    persona: str = "code"
    # The user's chosen execution mode remembered while a narrowing persona
    # (architect/ask) is active, so switching back to code/debug restores it.
    # None when the active persona narrows nothing. An explicit /mode <exec>
    # always wins and clears the restore point (and restores the write scope).
    persona_restore_mode: str | None = None
    # The user's base allow_write_globs snapshotted alongside
    # persona_restore_mode (may legitimately be None = unrestricted; the
    # restore-active signal is persona_restore_mode itself).
    persona_restore_write_globs: list[str] | None = None
    # Active persona scope, enforced independently from the user's base
    # allow_write_globs. A path must satisfy both scopes when both exist.
    persona_allow_write_globs: list[str] | None = None
    # Coordination cell for the switch_mode tool (interactive chat only; None
    # everywhere else, which also keeps the tool unregistered).
    persona_switch_state: PersonaSwitchState | None = None
    # Persona clients are keyed by the resolved client configuration that can
    # vary by role. Model-only caching would reuse the wrong temperature when
    # two roles intentionally share a model.
    persona_client_cache: dict[tuple[str, float], Any] | None = None
    persona_client_key: tuple[str, float] | None = None
    # Custom personas loaded from .alysis_personas / <user-config>/personas
    # (interactive chat only; builtins always win on name collisions).
    persona_registry: dict[str, Any] | None = None
    persona_registry_warnings: tuple[str, ...] = ()
    enable_chat_turn_step_budget: bool = False
    chat_turn_fixed_override: int | None = None
    verification_enabled: bool = True
    effective_verification_commands: list[str] = field(default_factory=list)
    authoritative_verification_commands: list[str] | None = None
    verification_selection_source: str = ""
    verification_selection_reason: str = ""
    verification_contract_type: str = ""
    verification_authoritative: bool = False
    verification_best_effort: bool = False
    deny_write_prefixes: list[str] | None = None
    allow_write_globs: list[str] | None = None
    session_log_dir_override: Path | None = None
    skills_enabled: bool = True
    skills_auto_invoke: bool = True
    skill_registry: dict[str, SkillBundle] | None = None
    skills_ordered: tuple[SkillBundle, ...] = ()
    skill_discovery_issues: tuple[Any, ...] = ()
    skill_catalog_entries: tuple[SkillCatalogEntry, ...] = ()
    repo_conventions: tuple[ConventionDocument, ...] = ()
    subagents_enabled: bool = False
    enforce_explicit_subagent_requests: bool = True
    subagent_depth: int = 0
    subagent_registry: dict[str, SubagentDefinition] | None = None
    child_scheduler: ChildScheduler | None = None
    steer_inbox: SteerInbox = field(default_factory=SteerInbox)
    child_repetition_signal: Callable[[dict[str, Any]], bool] | None = None
    read_ledger: SessionReadLedger | None = None
    step_system_message_provider: Callable[[], list[str]] | None = None
    step_system_message_delivery_observer: Callable[[int], None] | None = None
    step_budget_runtime: StepBudgetRuntime | None = None
    planner_workspace_context: dict[str, Any] | None = None
    workspace_grounding: _WorkspaceGroundingDescriptor | None = None
    focus_dir: Path | None = None
    focus_relpath: str = "."
    workspace_kind: str = "plain_dir"
    binding_requested_path: str | None = None
    binding_source: str | None = None
    binding_risk_level: str | None = None
    binding_created_path: bool | None = None
    active_workdir_relpath: str = "."
    session_source: str = "startup"
    session_source_metadata: dict[str, Any] = field(default_factory=dict)
    pinned_prefix_len: int = 0
    startup_context_baseline_tokens: int = 0
    request_context_measurement: RequestContextMeasurement | None = None
    workspace_touched_paths: set[str] = field(default_factory=set)
    workspace_writes_allowed: bool | None = None
    custom_tool_session_state: CustomToolSessionState | None = None
    hook_dispatcher: HookDispatcher | None = None
    execution_deadline: ExecutionDeadline | None = None
    cache_keepalive: ParentCacheKeepalive | None = None
    crash_diagnostics: CrashDiagnosticLogger | None = None
    crash_diagnostic_log_path: str | None = None
    agentbox_telemetry: AgentBoxTelemetry | None = None
    process_group_registry: ProcessGroupRegistry | None = None
    # Empty-response handling is budgeted per session, not per turn: two failed
    # recovery cycles mean the endpoint is not answering, and re-spending the
    # budget every turn would reintroduce the unbounded retrying this bounds.
    empty_response_stall_tracker: EmptyResponseStallTracker | None = None

    def workspace_write_contract_allows_writes(self) -> bool:
        """Return the effective write capability after role/tool filtering."""
        writes_allowed = self.workspace_writes_allowed
        if writes_allowed is None:
            writes_allowed = str(self.mode or "").strip().lower() != "readonly"
        return writes_allowed

    def initial_outstanding_action(self) -> str:
        """Describe the first unmet outcome allowed by this session's contract."""
        if self.workspace_write_contract_allows_writes():
            return "edit a relevant path"
        return "deliver the requested analysis or report"

    def __post_init__(self) -> None:
        self._bind_provider_retry_observer(self.client)
        if self.child_scheduler is None:
            subagent_tool = self.tools.get("subagent_run")
            launcher = getattr(getattr(subagent_tool, "run", None), "__self__", None)
            scheduler = getattr(launcher, "child_scheduler", None)
            if scheduler is not None:
                self.child_scheduler = scheduler
        if self.child_scheduler is not None:
            self.child_scheduler.set_parent_steer_inbox(self.steer_inbox)
        if self.cache_keepalive is None and self.subagent_depth == 0:
            cache_config = self.cfg.cache
            unsupported_reason = cache_keepalive_unsupported_reason(self.client)
            self.cache_keepalive = ParentCacheKeepalive(
                enabled=cache_config.keepalive_enabled,
                idle_threshold_s=cache_config.keepalive_idle_threshold_s,
                send_ping=self._send_cache_keepalive,
                on_disabled=self._on_cache_keepalive_disabled,
                on_unsupported=self._on_cache_keepalive_unsupported,
                unsupported_reason=unsupported_reason,
                deadline=self.execution_deadline,
            )

    def _bind_provider_retry_observer(self, client: Any) -> None:
        if client is None:
            return

        def _record_retry(payload: dict[str, object]) -> None:
            safe_payload = {
                "provider": str(payload.get("provider") or "unknown"),
                "attempt": max(1, int(payload.get("attempt") or 1)),
                "reason": str(payload.get("reason") or "provider_retry"),
                "elapsed_ms": max(0, int(payload.get("elapsed_ms") or 0)),
            }
            self.store.append("llm_call_retry", safe_payload)
            set_activity = getattr(self.surface, "set_model_retry_activity", None)
            if callable(set_activity):
                set_activity(safe_payload["attempt"])

        try:
            client._provider_retry_event_observer = _record_retry
        except Exception:  # noqa: BLE001 - diagnostics must not break a provider.
            pass

    def _reap_tracked_process_groups(self, *, event: ReapEvent) -> None:
        """Terminate or report the process groups this session's runner started.

        Runs on every turn exit path (success, honest-unverified, error,
        cancellation) and again at session close. Only groups the runner itself
        created and recorded are ever signalled; nothing is discovered by name
        or by scanning the process table.
        """
        registry = self.process_group_registry
        if registry is None:
            return
        try:
            decision = resolve_reap_decision(
                runtime_kind=self.runtime_kind,
                event=event,
                enabled=_process_reaping_enabled(self.cfg),
            )
            if decision.action is ReapAction.SKIP:
                return
            if decision.action is ReapAction.REPORT:
                survivors = survivor_payloads(registry)
                if survivors:
                    self.store.append(
                        "process_survivors",
                        {
                            "event": str(event),
                            "runtime_kind": self.runtime_kind.value,
                            "reason": decision.reason,
                            "count": len(survivors),
                            "groups": list(survivors),
                        },
                    )
                return
            for outcome in reap_tracked_groups(registry):
                self.store.append(
                    "process_reaped",
                    {
                        "event": str(event),
                        "runtime_kind": self.runtime_kind.value,
                        "reason": decision.reason,
                        **outcome.payload(),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - hygiene must never break the turn
            try:
                self.store.append(
                    "warning",
                    {"warning": "process_reaping_failed", "error": str(exc)},
                )
            except Exception:  # noqa: BLE001 - best-effort diagnostic
                pass

    def close(self, *, reason: str = "session_close") -> None:
        self._reap_tracked_process_groups(event=ReapEvent.SESSION_CLOSE)
        if self.cache_keepalive is not None:
            self.cache_keepalive.close()
        if self.child_scheduler is not None:
            try:
                self.child_scheduler.shutdown(cancel_pending=True)
            except Exception as exc:  # noqa: BLE001 - session teardown must continue
                self._hook_warning(
                    f"Child scheduler shutdown failed: {exc}",
                    code="child_scheduler_shutdown_failed",
                )
        if self.subagent_depth == 0:
            # Release the process-wide telemetry sink registered for this top-level run.
            set_provider_telemetry_sink(None)
        if self.terminal_manager is not None:
            try:
                self.terminal_manager.shutdown_all()
            except Exception as exc:  # noqa: BLE001
                # Session teardown must continue even if terminal shutdown hits an unexpected bug.
                self._hook_warning(
                    f"Terminal manager shutdown failed: {exc}",
                    code="terminal_shutdown_failed",
                )
        if self.durable_service_manager is not None:
            try:
                active_services = self.durable_service_manager.list_active()
            except Exception as exc:  # noqa: BLE001
                self._hook_warning(
                    f"Durable service status check failed during close: {exc}",
                    code="durable_service_status_failed",
                )
            else:
                if active_services:
                    self.store.append(
                        "durable_services_left_active",
                        {
                            "count": len(active_services),
                            "services": active_services,
                        },
                    )
        if not self._cache_efficiency_summary_recorded:
            cache_summary = self.usage_summary.cache_efficiency_summary()
            if int(cache_summary.get("reported_calls") or 0) > 0:
                self.store.append("cache_efficiency_summary", cache_summary)
            self._cache_efficiency_summary_recorded = True
        try:
            if self.crash_diagnostics is not None:
                run_finished_payload: dict[str, Any] = {
                    "status": reason,
                    "runtime_kind": self.runtime_kind.value,
                    "deadline": (
                        self.execution_deadline.telemetry_snapshot()
                        if self.execution_deadline is not None
                        else None
                    ),
                }
                if self.stop_reason:
                    # Only present when a path actually claimed a reason, so an
                    # ordinary run's event is byte-identical to before.
                    run_finished_payload["stop_reason"] = self.stop_reason
                self.crash_diagnostics.event(
                    "run_finished",
                    run_finished_payload,
                    durable=True,
                )
            cwd, active_workdir_relpath = self._hook_runtime_context()
            self._safe_dispatch_hooks(
                lambda: self.hook_dispatcher.fire_session_end(
                    cwd=cwd,
                    active_workdir_relpath=active_workdir_relpath,
                    payload={
                        "reason": reason,
                        "mode": self.mode,
                        "runtime_kind": self.runtime_kind.value,
                        "session_source": self.session_source,
                        "session_source_metadata": copy.deepcopy(self.session_source_metadata),
                        "workspace_root": os.fspath(self.root),
                        "focus_dir": os.fspath(self.focus_dir or self.root),
                        "focus_relpath": self.focus_relpath,
                        "active_workdir": os.fspath(cwd),
                        "active_workdir_relpath": active_workdir_relpath,
                        "workspace_kind": self.workspace_kind,
                        "usage_role": self.usage_role,
                        "message_count": len(self.messages),
                        "pinned_prefix_len": self.pinned_prefix_len,
                        "subagent_depth": self.subagent_depth,
                        "skills_enabled": self.skills_enabled,
                        "subagents_enabled": self.subagents_enabled,
                    },
                )
            )
            if self.mcp_manager is not None:
                self.mcp_manager.close()
        finally:
            if self.agentbox_telemetry is not None:
                self.agentbox_telemetry.close(error=reason not in {"session_close", "completed"})
            self.store.close()

    def _hook_warning(self, message: str, *, code: str = "hook_warning") -> None:
        clean = str(message or "").strip()
        if not clean:
            return
        self.store.append("warning", {"warning": code, "message": clean})
        surface_on_warning = _meaningful_surface_warning_handler(self.surface)
        if callable(surface_on_warning):
            surface_on_warning(clean)
        else:
            warnings.warn(clean, stacklevel=2)

    def _safe_dispatch_hooks(
        self,
        dispatcher_call: Callable[[], HookDispatchResult],
    ) -> HookDispatchResult:
        if self.hook_dispatcher is None:
            return HookDispatchResult()
        try:
            result = dispatcher_call()
        except Exception as exc:  # noqa: BLE001
            self._hook_warning(
                f"Lifecycle hook dispatch failed: {exc}",
                code="hook_dispatch_failed",
            )
            return HookDispatchResult()
        for notice in result.system_notices:
            self._hook_notice(notice)
        return result

    def _hook_notice(self, message: str) -> None:
        clean = str(message or "").strip()
        if not clean:
            return
        self.store.append("hook_notice", {"message": clean})
        handler = getattr(self.surface, "on_notice", None)
        if callable(handler):
            handler(clean)
            return
        fallback = _meaningful_surface_legacy_warning_handler(self.surface)
        if callable(fallback):
            fallback(clean)

    def _hook_runtime_context(self) -> tuple[Path, str]:
        return (
            resolve_session_active_workdir_path(self),
            resolve_session_active_workdir_relpath(self),
        )

    def _append_hook_messages(
        self,
        *,
        event_name: str,
        system_messages: Iterable[str] = (),
        user_messages: Iterable[str] = (),
        pinned: bool = False,
    ) -> int:
        appended_count = 0
        for role, messages in (("system", system_messages), ("user", user_messages)):
            for raw_message in messages:
                text = str(raw_message or "").strip()
                if not text:
                    continue
                self.messages.append({"role": role, "content": text})
                appended_count += 1
                self.store.append(
                    "hook_message_added",
                    {
                        "event_name": event_name,
                        "role": role,
                        "chars": len(text),
                        "pinned": pinned,
                    },
                )
        if pinned and appended_count > 0:
            self.pinned_prefix_len += appended_count
        return appended_count

    def context_left(self) -> ContextLeft:
        compaction_settings = resolve_compaction_settings(self.cfg)
        effective_tool_list = effective_tools_for_client(self.client, self.tool_list)
        startup_baseline_tokens = self.startup_context_baseline_tokens
        if self.startup_messages:
            # Tool support can change after the provider rejects a tool-bearing
            # request. Keep the dynamic HUD baseline aligned with what the
            # client's current transport state will actually send.
            startup_baseline_tokens = estimate_request_token_breakdown(
                messages=self.startup_messages,
                tool_list=effective_tool_list,
                pinned_prefix_len=self.pinned_prefix_len,
            ).total_tokens
        usage_context = usage_context_from_client_response(
            client=self.client,
            response=None,
            operation="main_llm",
        )
        request_measurement = self.request_context_measurement
        if request_measurement is not None and not request_measurement.matches_route(
            requested_model=self.client.model,
            provider_key=usage_context.get("provider_key"),
            protocol=usage_context.get("protocol"),
            base_url_host=usage_context.get("base_url_host"),
        ):
            request_measurement = None
        calibration = self.usage_summary.recent_calibration_snapshot(
            requested_model=self.client.model,
            provider_key=usage_context.get("provider_key"),
            protocol=usage_context.get("protocol"),
            base_url_host=usage_context.get("base_url_host"),
            operation="main_llm",
            request_mode=(request_measurement.request_mode if request_measurement else None),
            cache_strategy=(request_measurement.cache_strategy if request_measurement else None),
            limit=20,
        )
        estimate_multiplier = calibration.get("prompt_estimate_error_ratio_p90")
        return compute_context_left(
            messages=self.messages,
            model_name=self.client.model,
            registry=self.model_registry,
            tool_list=effective_tool_list,
            pinned_prefix_len=self.pinned_prefix_len,
            safety_margin_tokens=compaction_settings.safety_margin_tokens,
            startup_baseline_tokens=startup_baseline_tokens,
            prompt_estimate_multiplier=(
                float(estimate_multiplier) if isinstance(estimate_multiplier, int | float) else None
            ),
            request_measurement=request_measurement,
        )

    def refresh_compactor_calibration_filters(self) -> None:
        compactor = self.conversation_compactor
        updater = getattr(compactor, "update_calibration_filters", None)
        if not callable(updater):
            return
        updater(
            usage_context_from_client_response(
                client=self.client,
                response=None,
                operation="main_llm",
            )
        )

    def invalidate_request_context(self, *, reason: str) -> None:
        if self.request_context_measurement is None:
            return
        self.request_context_measurement = None
        self.store.append(
            "request_context_invalidated",
            {"reason": str(reason or "request_shape_changed")},
        )

    @staticmethod
    def _normalize_visible_assistant_text(text: str) -> str:
        return str(text or "").strip()

    def _emit_assistant_message_if_changed(
        self,
        *,
        text: str,
        prior_visible_text: str = "",
        extra_payload: dict[str, Any] | None = None,
        streamed_text_emitted: bool = False,
    ) -> str:
        normalized_text = self._normalize_visible_assistant_text(text)
        if not normalized_text:
            if extra_payload:
                payload = {"content": text}
                payload.update(extra_payload)
                self.store.append("assistant_message", payload)
            return self._normalize_visible_assistant_text(prior_visible_text)
        if normalized_text == self._normalize_visible_assistant_text(prior_visible_text):
            if extra_payload:
                payload = {"content": text}
                payload.update(extra_payload)
                self.store.append("assistant_message", payload)
            return normalized_text
        payload = {"content": text}
        if extra_payload:
            payload.update(extra_payload)
        self.store.append("assistant_message", payload)
        _emit_assistant_message_events(
            self.surface,
            text,
            streamed_text_emitted=streamed_text_emitted,
        )
        if _legacy_message_tool_events_required(self.surface):
            self.surface.on_assistant_message_done(text)
        return normalized_text

    def _record_llm_usage(
        self,
        *,
        client: Any,
        response: Any,
        messages: list[dict[str, Any]],
        tool_list: list[dict[str, Any]] | None,
        operation: str,
        role_override: str | None = None,
    ) -> Any | None:
        """Record one provider call without allowing telemetry to break the turn."""
        if response is None:
            return None
        try:
            tool_list = effective_tools_for_client(client, tool_list)
            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
            usage_context = usage_context_from_client_response(
                client=client,
                response=response,
                operation=operation,
            )
            prompt_token_source = str(
                usage_context.get("api_usage_source_detail") or UsageSource.PROVIDER_RESPONSE.value
            )
            prompt_token_confidence = str(
                usage_context.get("api_usage_confidence") or UsageConfidence.REPORTED.value
            )
            if prompt_tokens is None:
                try:
                    counted_input = count_input_tokens_if_supported(
                        client=client,
                        messages=list(messages or []),
                        tools=tool_list,
                    )
                except Exception as exc:  # noqa: BLE001 -- fallback must not break a turn
                    self.store.append(
                        "warning",
                        {
                            "warning": "provider_input_token_count_failed",
                            "operation": operation,
                            "error": str(exc),
                        },
                    )
                else:
                    if counted_input is not None:
                        prompt_tokens = counted_input.input_tokens
                        usage_context["api_usage_source_detail"] = counted_input.source.value
                        usage_context["api_usage_confidence"] = counted_input.confidence.value
                        usage_context["api_prompt_tokens_authoritative"] = (
                            counted_input.confidence.value == "authoritative"
                        )
                        prompt_token_source = counted_input.source.value
                        prompt_token_confidence = counted_input.confidence.value
            response_tool_calls = [
                {
                    "id": getattr(tool_call, "id", ""),
                    "name": getattr(tool_call, "name", ""),
                    "arguments": getattr(tool_call, "arguments", {}),
                }
                for tool_call in (getattr(response, "tool_calls", None) or [])
            ]
            usage_record = build_usage_record(
                role=role_override or self.usage_role,
                requested_model=getattr(client, "model", None) or self.client.model,
                response_model=getattr(response, "response_model", None),
                messages=list(messages or []),
                response_content=str(getattr(response, "content", "") or ""),
                response_tool_calls=response_tool_calls,
                api_prompt_tokens=prompt_tokens,
                api_completion_tokens=(
                    getattr(usage, "completion_tokens", None) if usage else None
                ),
                api_total_tokens=(getattr(usage, "total_tokens", None) if usage else None),
                api_usage=usage,
                api_cached_prompt_tokens=(
                    getattr(usage, "cached_prompt_tokens", None) if usage else None
                ),
                tool_list=tool_list,
                pinned_prefix_len=self.pinned_prefix_len,
                registry=self.model_registry,
                **usage_context,
            )
            self.usage_summary.add_record(usage_record)
            if operation in {"main_llm", "context_overflow_retry"}:
                if prompt_tokens is None or usage_record.prompt_tokens != prompt_tokens:
                    prompt_token_source = UsageSource.LOCAL_ESTIMATE.value
                    prompt_token_confidence = UsageConfidence.ESTIMATED.value
                measurement_tools = tool_list
                request_plan = usage_record.request_plan or {}
                try:
                    planned_tool_count = int(request_plan.get("tool_count"))
                except (TypeError, ValueError):
                    planned_tool_count = -1
                if planned_tool_count == 0:
                    measurement_tools = None
                self.request_context_measurement = RequestContextMeasurement(
                    input_tokens=max(0, usage_record.prompt_tokens),
                    anchor_estimate_tokens=estimate_request_tokens(
                        list(messages or []),
                        measurement_tools,
                    ),
                    persistent_anchor_estimate_tokens=estimate_request_tokens(
                        self.messages,
                        measurement_tools,
                    ),
                    source=prompt_token_source,
                    confidence=prompt_token_confidence,
                    requested_model=usage_record.requested_model,
                    provider_key=usage_record.provider_key,
                    protocol=usage_record.protocol,
                    base_url_host=usage_record.base_url_host,
                    operation=usage_record.operation,
                    request_mode=usage_record.request_mode,
                    cache_strategy=usage_record.cache_strategy,
                    request_message_signatures=request_message_signatures(list(messages or [])),
                    persistent_message_signatures=request_message_signatures(self.messages),
                    tool_schema_signature=tool_schema_signature(measurement_tools),
                    request_has_media=request_contains_media(list(messages or [])),
                    persistent_has_media=request_contains_media(self.messages),
                )
            self.store.append("llm_usage", usage_record.to_payload())
            if self.agentbox_telemetry is not None:
                self.agentbox_telemetry.record_usage(usage_record)
            return usage_record
        except Exception as exc:  # noqa: BLE001 -- accounting cannot break the agent turn
            self.store.append(
                "warning",
                {
                    "warning": "llm_usage_record_failed",
                    "operation": operation,
                    "error": str(exc),
                },
            )
            return None

    def _send_cache_keepalive(
        self,
        request: CacheKeepaliveRequest,
        stop_event: Any,
    ) -> bool:
        """Replay the exact parent prefix and discard the bounded response."""
        tools = effective_tools_for_client(self.client, request.tools)
        tool_choice = (
            copy.deepcopy(request.tool_choice)
            if tools is not None and request.tool_choice is not None
            else None
        )
        telemetry_count_before = provider_telemetry_record_count()
        try:
            with provider_telemetry_operation("cache_keepalive"):
                response = _main_agent_chat(
                    client=self.client,
                    messages=copy.deepcopy(request.messages),
                    tools=copy.deepcopy(tools),
                    stream=False,
                    on_text_delta=None,
                    temperature=0.0,
                    max_tokens=16,
                    cancellation_token=EventCancellationToken(stop_event),
                    tool_choice=tool_choice,
                )
        except Exception as exc:  # noqa: BLE001 - affinity is best-effort only
            if provider_telemetry_record_count() == telemetry_count_before:
                ProviderCallTelemetryRecorder(
                    provider_key=getattr(self.client, "provider_key", None),
                    protocol=str(getattr(self.client, "protocol", "unknown") or "unknown"),
                    model=str(getattr(self.client, "model", "") or "unknown"),
                    base_url=str(getattr(self.client, "base_url", "") or ""),
                    stream=False,
                    tools=tools,
                    operation="cache_keepalive",
                ).record_error(exc)
            self.store.append(
                "cache_keepalive",
                {
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "error": sanitize_error_text_for_output(exc),
                    "request": {
                        "message_count": len(request.messages),
                        "tool_count": len(tools or []),
                        "stream": False,
                        "temperature": 0.0,
                        "max_tokens": 16,
                        "tool_choice_present": tool_choice is not None,
                    },
                },
            )
            return False
        usage_record = self._record_llm_usage(
            client=self.client,
            response=response,
            messages=request.messages,
            tool_list=tools,
            operation="cache_keepalive",
            role_override=f"{self.usage_role}:cache_keepalive",
        )
        self.store.append(
            "cache_keepalive",
            {
                "status": "completed",
                "request": {
                    "message_count": len(request.messages),
                    "tool_count": len(tools or []),
                    "stream": False,
                    "temperature": 0.0,
                    "max_tokens": 16,
                    "tool_choice_present": tool_choice is not None,
                },
                "usage": usage_record.to_payload() if usage_record is not None else None,
            },
        )
        return True

    def _on_cache_keepalive_disabled(self, consecutive_failures: int) -> None:
        self.store.append(
            "warning",
            {
                "warning": "cache_keepalive_disabled_after_failures",
                "consecutive_failures": consecutive_failures,
                "message": (
                    "Cache keepalive disabled for this session after repeated failures; "
                    "child execution continues unchanged."
                ),
            },
        )

    def _on_cache_keepalive_unsupported(self, reason: str) -> None:
        self.store.append(
            "warning",
            {
                "warning": "keepalive_unsupported_transport",
                "reason": reason,
                "message": (
                    "Cache keepalive is disabled because this transport does not replay the "
                    "parent's active prompt-cache stream."
                ),
            },
        )

    def _emit_final_assistant_text(
        self,
        *,
        final_text: str,
        assistant_response: Any | None = None,
        language: str = "",
        script: str = "",
        explicit_language_override: bool = False,
        prior_visible_text: str = "",
        streamed_text_emitted: bool = False,
        final_event_payload: dict[str, Any] | None = None,
        internal_fallback: bool = False,
        internal_fallback_kind: str = "",
    ) -> str:
        emitted_text = str(final_text or "").strip()
        emitted_text, rewrite_payload = _rewrite_final_summary_for_language(
            client=self.client,
            final_text=emitted_text,
            language=language,
            script=script,
            explicit_language_override=explicit_language_override,
            record_usage=lambda **kw: self._record_llm_usage(client=self.client, **kw),
        )
        if rewrite_payload is not None:
            self.store.append("final_summary_rewrite", rewrite_payload)
        assistant_message = None
        if assistant_response is not None:
            candidate_message = assistant_message_from_response(
                assistant_response,
                content=emitted_text,
            )
            if PROVIDER_METADATA_KEY in candidate_message:
                assistant_message = candidate_message
        extra_payload = {"message": assistant_message} if assistant_message is not None else None
        if internal_fallback and self.subagent_depth > 0:
            # A nested run's locally generated stop report is internal state. The
            # nested surface forwards assistant messages up to the parent's panel,
            # so emitting it here would render the dump to the user even though the
            # tool result never carries it. Record it; do not show it.
            self.store.append(
                "assistant_message",
                {"content": emitted_text, "internal_fallback": True},
            )
        else:
            self._emit_assistant_message_if_changed(
                text=emitted_text,
                prior_visible_text=prior_visible_text,
                extra_payload=extra_payload,
                streamed_text_emitted=streamed_text_emitted,
            )
        if assistant_message is not None:
            if internal_fallback:
                # Today a fallback always arrives with assistant_response=None, so
                # nothing is appended and this does not fire. It is here because the
                # invariant is "an internal artifact that enters the transcript is
                # marked", and that has to hold at the point of entry, not by anyone
                # remembering to re-check later.
                mark_message_internal(
                    assistant_message,
                    kind=internal_fallback_kind or "forced_final_summary_fallback",
                )
            self.messages.append(assistant_message)
        final_payload: dict[str, Any] = {"content": emitted_text}
        if internal_fallback:
            # A locally generated stop report, not a model answer. Recorded as a
            # fact here so the subagent boundary can refuse to hand it to a parent
            # as a deliverable, without inspecting the text.
            final_payload["internal_fallback"] = True
            final_payload["artifact_visibility"] = ArtifactVisibility.INTERNAL.value
            if internal_fallback_kind:
                final_payload["internal_fallback_kind"] = internal_fallback_kind
        self.store.append(
            "final",
            _add_event_diagnostics(final_payload, final_event_payload),
        )
        return emitted_text

    def _forced_final_summary_activity_snapshot(self) -> dict[str, Any]:
        tool_calls_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
        read_paths: list[str] = []
        listed_paths: list[str] = []
        edited_paths: list[str] = []
        verification_commands: list[str] = []
        shell_commands: list[str] = []
        other_actions: list[str] = []
        failed_actions: list[str] = []

        def _append_unique(items: list[str], value: str) -> None:
            clean = str(value or "").strip()
            if clean and clean not in items:
                items.append(clean)

        def _path_arg(args: dict[str, Any]) -> str:
            for key in ("path", "file", "target", "target_path"):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        def _command_arg(args: dict[str, Any]) -> str:
            for key in ("command", "cmd"):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        for message in self.messages:
            role = str(message.get("role") or "")
            if role == "assistant":
                for raw_call in message.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    call_id = str(raw_call.get("id") or "").strip()
                    function = raw_call.get("function")
                    if not call_id or not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "").strip()
                    raw_args = function.get("arguments")
                    args: dict[str, Any] = {}
                    if isinstance(raw_args, str) and raw_args.strip():
                        try:
                            parsed_args = json.loads(raw_args)
                        except Exception:  # noqa: BLE001
                            parsed_args = None
                        if isinstance(parsed_args, dict):
                            args = parsed_args
                    if name:
                        tool_calls_by_id[call_id] = (name, args)
                continue
            if role != "tool":
                continue
            call_id = str(message.get("tool_call_id") or "").strip()
            name, args = tool_calls_by_id.get(call_id, ("", {}))
            if not name:
                continue
            content = message.get("content")
            result: Any = None
            if isinstance(content, str) and content.strip():
                try:
                    result = json.loads(content)
                except Exception:  # noqa: BLE001
                    result = None
            failed = isinstance(result, dict) and "error" in result
            path = _path_arg(args)
            command = _command_arg(args)
            if failed:
                label = f"{name} {path}".strip() if path else name
                _append_unique(failed_actions, label)
                continue
            if name == "fs_read" and path:
                _append_unique(read_paths, path)
            elif name == "fs_list" and path:
                _append_unique(listed_paths, path)
            elif name in {"fs_write", "fs_edit", "apply_patch"} and path:
                _append_unique(edited_paths, path)
            elif name in {"shell", "shell_command", "shell_run", "verify_run"} and command:
                if name == "verify_run":
                    _append_unique(verification_commands, command)
                else:
                    _append_unique(shell_commands, command)
            else:
                label = f"{name} {path}".strip() if path else name
                _append_unique(other_actions, label)

        return {
            "read_paths": read_paths,
            "listed_paths": listed_paths,
            "edited_paths": edited_paths,
            "verification_commands": verification_commands,
            "shell_commands": shell_commands,
            "other_actions": other_actions,
            "failed_actions": failed_actions,
        }

    def _forced_final_summary_fallback_text(
        self,
        *,
        termination_cause: str,
        termination_kind: str = "step_budget_exhausted",
        max_steps: int | None,
        fallback_reason: str,
        latest_assistant_text: str = "",
    ) -> str:
        snapshot = self._forced_final_summary_activity_snapshot()

        def _join_limited(items: list[str], *, limit: int = 8) -> str:
            visible = items[:limit]
            text = ", ".join(visible)
            remaining = len(items) - len(visible)
            if remaining > 0:
                text += f", and {remaining} more"
            return text

        completed: list[str] = []
        read_paths = snapshot["read_paths"]
        listed_paths = snapshot["listed_paths"]
        edited_paths = snapshot["edited_paths"]
        verification_commands = snapshot["verification_commands"]
        shell_commands = snapshot["shell_commands"]
        other_actions = snapshot["other_actions"]
        failed_actions = snapshot["failed_actions"]

        if read_paths:
            completed.append(f"- Read files: {_join_limited(read_paths)}.")
        if listed_paths:
            completed.append(f"- Listed directories: {_join_limited(listed_paths)}.")
        if edited_paths:
            completed.append(f"- Edited files: {_join_limited(edited_paths)}.")
        if verification_commands:
            completed.append(
                f"- Ran verification: {_join_limited(verification_commands, limit=4)}."
            )
        if shell_commands:
            completed.append(f"- Ran shell commands: {_join_limited(shell_commands, limit=4)}.")
        if other_actions:
            completed.append(f"- Ran tools: {_join_limited(other_actions)}.")
        latest = str(latest_assistant_text or "").strip()
        if latest:
            latest = " ".join(latest.split())
            if len(latest) > 180:
                latest = latest[:177].rstrip() + "..."
            completed.append(f"- Last assistant progress note: {latest}")
        if not completed:
            completed.append(
                "- No durable repository change was completed before the turn stopped."
            )

        remaining = [
            "- Continue from the recorded tool results instead of restarting from scratch.",
        ]
        writes_allowed = self.workspace_write_contract_allows_writes()
        if not edited_paths and writes_allowed:
            remaining.append(
                "- Implementation has not started yet; identify the smallest safe fix first."
            )
        elif not edited_paths:
            remaining.append("- Deliver the requested analysis or report.")
        if edited_paths and not verification_commands:
            remaining.append("- Run focused verification for the edited files before finalizing.")
        if failed_actions:
            remaining.append(
                f"- Resolve failed tool calls: {_join_limited(failed_actions, limit=5)}."
            )
        if writes_allowed:
            remaining.append("- Finish the requested implementation or report a concrete blocker.")
        else:
            remaining.append("- Report a concrete blocker if the requested result cannot be delivered.")

        kind = _normalize_forced_summary_termination_kind(termination_kind)
        if kind == ForcedFinalSummaryTerminationKind.STEP_BUDGET_EXHAUSTED:
            if max_steps is None:
                stop_risk = "- The turn stopped before completion."
            else:
                stop_risk = f"- The turn exhausted its {max_steps}-step limit before completion."
        elif kind == ForcedFinalSummaryTerminationKind.COMPLETION_GATE_STAGNATION:
            stop_risk = (
                "- Execution stopped after repeated invalid finalization attempts without "
                "new implementation or verification progress."
            )
        elif kind == ForcedFinalSummaryTerminationKind.EXECUTION_GUARD_STAGNATION:
            stop_risk = (
                "- Execution stopped after a runtime guard observed repeated no-progress behavior."
            )
        elif kind == ForcedFinalSummaryTerminationKind.DEADLINE_EXHAUSTED:
            stop_risk = "- The run deadline was exhausted before the turn could finish."
        else:
            stop_risk = "- The turn stopped before completion for the reported reason."
        risks = [
            stop_risk,
            "- This fallback was generated from runtime state before the turn terminated.",
        ]
        if not verification_commands:
            risks.append("- No verification result was recorded in this turn.")

        return (
            f"The turn stopped before it could finish ({termination_cause}).\n\n"
            "Completed work:\n" + "\n".join(completed) + "\n\n"
            "Remaining work:\n" + "\n".join(remaining) + "\n\n"
            "Known issues or risks:\n" + "\n".join(risks)
        )

    def _emit_forced_final_summary_before_termination(
        self,
        *,
        reason: str,
        termination_cause: str,
        termination_kind: str = "step_budget_exhausted",
        max_steps: int | None,
        language: str = "",
        script: str = "",
        explicit_language_override: bool = False,
        latest_assistant_text: str = "",
        allow_llm_summary: bool = True,
        local_summary_override: str = "",
        final_event_payload: dict[str, Any] | None = None,
    ) -> str:
        normalized_termination_kind = _normalize_forced_summary_termination_kind(
            termination_kind
        ).value
        # Internal artifacts (a previous turn's locally generated stop report) are
        # excluded here by marker, so a summary can never be built by re-narrating
        # one. Marker-based, never a scan of the text.
        request_messages = summary_input_messages(self.messages)
        latest_assistant_text = str(latest_assistant_text or "").strip()
        if latest_assistant_text:
            request_messages.append({"role": "assistant", "content": latest_assistant_text})
        request_messages.append(
            {
                "role": "system",
                "content": _FORCED_FINAL_SUMMARY_SYSTEM_PROMPT_TEMPLATE.format(
                    termination_cause=termination_cause
                ),
            }
        )
        self.store.append(
            "forced_final_summary_requested",
            {
                "reason": reason,
                "termination_cause": termination_cause,
                "termination_kind": normalized_termination_kind,
                "max_steps": max_steps,
            },
        )

        final_text = ""
        fallback_reason: str | None = None
        fallback_error: str | None = None
        resp = None
        deadline = self.execution_deadline
        if not allow_llm_summary or (
            deadline is not None and not deadline.can_start(MINIMUM_FORCED_SUMMARY_SECONDS)
        ):
            fallback_reason = "local_summary_due_to_deadline"
        else:
            try:
                with temporarily_clamp_client_timeout(
                    self.client,
                    deadline,
                    operation="forced_final_summary_llm",
                ):
                    resp = _main_agent_chat(
                        client=self.client,
                        messages=request_messages,
                        tools=None,
                        stream=False,
                        on_text_delta=None,
                    )
            except DeadlineExhausted:
                fallback_reason = "local_summary_due_to_deadline"
            except Exception as exc:  # noqa: BLE001
                fallback_reason = "finalization_error"
                fallback_error = str(exc)
            else:
                self._record_llm_usage(
                    client=self.client,
                    response=resp,
                    messages=request_messages,
                    tool_list=None,
                    operation="forced_final_summary_llm",
                )
                final_text = str(resp.content or "").strip()
                if resp.tool_calls:
                    fallback_reason = "tool_call_response"
                elif not final_text:
                    fallback_reason = "blank_response"
                elif _looks_like_unexecuted_tool_call_markup(final_text):
                    fallback_reason = "tool_call_markup_response"

        if fallback_reason is not None:
            # A caller that already built a factual account of what it salvaged
            # supplies it here, so the local summary reports real state instead
            # of the generic termination notice.
            final_text = str(local_summary_override or "").strip()
            if not final_text:
                final_text = self._forced_final_summary_fallback_text(
                    termination_cause=termination_cause,
                    termination_kind=normalized_termination_kind,
                    max_steps=max_steps,
                    fallback_reason=fallback_reason,
                    latest_assistant_text=latest_assistant_text,
                )
            fallback_payload: dict[str, Any] = {
                "reason": reason,
                "termination_cause": termination_cause,
                "termination_kind": normalized_termination_kind,
                "max_steps": max_steps,
                "fallback_reason": fallback_reason,
            }
            if fallback_error:
                fallback_payload["error"] = fallback_error
            _add_event_diagnostics(fallback_payload, final_event_payload)
            self.store.append("forced_final_summary_fallback", fallback_payload)

        emitted_text = self._emit_final_assistant_text(
            final_text=final_text,
            assistant_response=resp if fallback_reason is None else None,
            internal_fallback=fallback_reason is not None,
            internal_fallback_kind=normalized_termination_kind,
            language=language,
            script=script,
            explicit_language_override=explicit_language_override,
            # Thread the turn's last-shown answer so the change-dedup can suppress a
            # forced summary that merely repeats it (otherwise prior_visible_text
            # defaults to "" and on_assistant_message_done always re-fires).
            prior_visible_text=latest_assistant_text,
            final_event_payload=final_event_payload,
        )
        if fallback_reason is None:
            completed_payload = {
                "reason": reason,
                "termination_cause": termination_cause,
                "termination_kind": normalized_termination_kind,
                "max_steps": max_steps,
                "content_length": len(emitted_text),
            }
            _add_event_diagnostics(completed_payload, final_event_payload)
            self.store.append("forced_final_summary_completed", completed_payload)
        return emitted_text

    def run_turn(
        self,
        instruction: str,
        *,
        image_paths: list[str] | None = None,
        routing_mode_override: str | None = None,
        ephemeral_system_messages: list[str] | tuple[str, ...] | None = None,
        ephemeral_user_messages: list[str] | tuple[str, ...] | None = None,
        cancellation_token: Any | None = None,
        chat_only: bool = False,
    ) -> int:
        turn_thread_id = threading.get_ident()
        self._turn_owner_thread_id = turn_thread_id
        self._bind_provider_retry_observer(self.client)
        try:
            if self.agentbox_telemetry is None:
                return _run_turn(
                    self,
                    instruction,
                    image_paths=image_paths,
                    routing_mode_override=routing_mode_override,
                    ephemeral_system_messages=ephemeral_system_messages,
                    ephemeral_user_messages=ephemeral_user_messages,
                    cancellation_token=cancellation_token,
                    chat_only=chat_only,
                )
            self.agentbox_telemetry.task(instruction)
            with self.agentbox_telemetry.turn():
                return _run_turn(
                    self,
                    instruction,
                    image_paths=image_paths,
                    routing_mode_override=routing_mode_override,
                    ephemeral_system_messages=ephemeral_system_messages,
                    ephemeral_user_messages=ephemeral_user_messages,
                    cancellation_token=cancellation_token,
                    chat_only=chat_only,
                )
        except CooperativeCancellationError as exc:
            # The budget watchdog cancels through the same cooperative channel a
            # user does, so the two are told apart by reason. A budget stop is a
            # normal outcome and is finalized cleanly; a user cancellation and
            # every other cooperative stop fall through to the failure boundary
            # below, unchanged.
            if not is_budget_cancellation(exc):
                self._emit_terminal_error(exc)
                raise
            return self._finalize_run_budget_stop()
        except Exception as exc:
            # Single authoritative terminal-failure boundary: every caller (one-shot
            # run, interactive chat, Forge workers, subagents) routes turns through
            # here, so one durable, redacted record makes a crashed build
            # reconstructable from artifacts alone. Re-raise unchanged afterwards.
            self._emit_terminal_error(exc)
            raise
        finally:
            # Same reasoning as the boundary above: this is the only per-turn point
            # that sees normal returns, exceptions, and cancellation, so turn-level
            # process hygiene belongs here rather than in the loop's success path.
            try:
                if (
                    cancellation_token is not None
                    and bool(getattr(cancellation_token, "is_cancelled", False))
                    and self.child_scheduler is not None
                ):
                    pending_run_ids = self.child_scheduler.pending_run_ids()
                    if pending_run_ids:
                        self.child_scheduler.cancel(
                            run_id=pending_run_ids,
                            wait_for_running=True,
                            # Unbounded while there is budget left, as before.
                            # Once it is gone this join has nothing to wait
                            # with, and a child that ignores its cancellation
                            # would otherwise pin the run open indefinitely --
                            # on a path that only became reachable now that a
                            # budget stop cancels cooperatively.
                            wait_timeout_s=(
                                resolve_budget_grace_seconds()
                                if (
                                    self.execution_deadline is not None
                                    and self.execution_deadline.is_exhausted()
                                )
                                else None
                            ),
                        )
                        self.store.append(
                            "subagent_turn_end_enforcement",
                            {
                                "policy": str(self.cfg.subagent_orchestration.turn_end_policy),
                                "action": "parent_cancel",
                                "run_ids": pending_run_ids,
                            },
                        )
            finally:
                try:
                    self._reap_tracked_process_groups(event=ReapEvent.TURN_FINALIZATION)
                finally:
                    if getattr(self, "_turn_owner_thread_id", None) == turn_thread_id:
                        self._turn_owner_thread_id = None

    def _finalize_run_budget_stop(self) -> int:
        """Finalize a watchdog-cancelled run as a clean budget stop.

        This is the backstop, not the common path. The turn engine's
        ``_deadline_exhausted_result`` handles a budget stop the run noticed
        itself and is richer, because it can still salvage workspace state.
        This runs only when the run was blocked somewhere no cooperative
        checkpoint could reach and had to be cancelled from outside: record the
        same markers, emit the same local (never model-generated) summary, and
        exit zero, because running out of time is an outcome and not a crash.
        """
        self.stop_reason = STOP_REASON_RUN_BUDGET_EXHAUSTED
        payload: dict[str, Any] = {
            "operation": "budget_watchdog",
            "stop_reason": STOP_REASON_RUN_BUDGET_EXHAUSTED,
            "deadline_exhausted": True,
            "deadline": (
                self.execution_deadline.telemetry_snapshot()
                if self.execution_deadline is not None
                else None
            ),
        }
        self.store.append("deadline_exhausted", payload)
        if self.crash_diagnostics is not None:
            self.crash_diagnostics.event("deadline_exhausted", payload, durable=True)
        try:
            self._emit_forced_final_summary_before_termination(
                reason="deadline_exhausted",
                termination_cause="the run deadline is exhausted",
                termination_kind="deadline_exhausted",
                max_steps=None,
                # No LLM call: the budget is already gone, and this PR adds no
                # model-visible traffic on the stop path.
                allow_llm_summary=False,
                final_event_payload={
                    "degraded": False,
                    "degraded_reason": STOP_REASON_RUN_BUDGET_EXHAUSTED,
                    "stop_reason": STOP_REASON_RUN_BUDGET_EXHAUSTED,
                },
            )
        except Exception as exc:  # noqa: BLE001 - a clean stop must stay clean
            self.store.append(
                "warning",
                {"warning": "budget_stop_summary_failed", "error": str(exc)},
            )
        return BUDGET_STOP_EXIT_CODE

    def _emit_terminal_error(
        self,
        error: BaseException,
        *,
        operation: str = "run_turn",
    ) -> None:
        """Record one redacted, joinable terminal-failure record for ``error``.

        Written to the per-run store (default-on; suppressed only by ``--no-log``) and,
        when the opt-in crash-diagnostic log is enabled, as the durable ``terminal_error``
        event. Never raises — a diagnostic failure here must not mask the user's error.
        """
        try:
            fields = build_error_event_fields(error, operation=operation)
        except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
            fields = {"error_type": type(error).__name__, "operation": operation}
        try:
            self.store.append("terminal_error", dict(fields))
        except Exception:  # noqa: BLE001 - best-effort durable record
            pass
        if self.crash_diagnostics is not None:
            try:
                self.crash_diagnostics.event("terminal_error", dict(fields), durable=True)
            except Exception:  # noqa: BLE001 - best-effort diagnostic event
                pass


def _shell_command_probe(*, shell_runner: Any, root: Path) -> ShellCommandRunner:
    """Adapt the session's shell runner to the provisioning probe interface.

    Everything provisioning does goes through here, so it inherits the session's
    sandbox, PATH, and environment -- the same ones the agent's own test command
    will resolve. A disabled or unbuildable runner yields "could not run", which
    the decision reads as unknown rather than as a missing package.
    """

    def _run(command: str, timeout_s: float) -> ShellProbeResult:
        if shell_runner is None:
            return ShellProbeResult(exit_code=None, stderr="shell execution is unavailable")
        try:
            completed = shell_runner.run(
                root=root,
                cwd=root,
                cmd=command,
                timeout_s=int(timeout_s),
            )
        except Exception as exc:  # noqa: BLE001 - readonly mode, sandbox failure, timeout
            return ShellProbeResult(exit_code=None, stderr=str(exc))
        return ShellProbeResult(
            exit_code=int(getattr(completed, "returncode", 1) or 0),
            stderr=str(getattr(completed, "stderr", "") or ""),
        )

    return _run


def _pre_provision_declared_test_runner(
    *,
    store: SessionStore,
    root: Path,
    cfg: AppConfig,
    runtime_kind: RuntimeKind,
    subagent_depth: int,
    shell_runner: Any,
) -> None:
    """Close a declared-but-missing test runner gap once, during warmup.

    A top-level autonomous run installs the runner the repo's own config
    declares; an interactive run only records that the gap exists so the agent
    or the user can decide. Never raises: startup must survive a broken package
    index, a disabled shell, or an unreachable sandbox.
    """
    try:
        enabled = _workspace_provisioning_enabled(cfg)
        declared = detect_declared_test_runner(root) if enabled else None
        importable: bool | None = None
        if declared is not None:
            importable = probe_runner_importable(
                declared.package,
                run_command=_shell_command_probe(shell_runner=shell_runner, root=root),
            )
        decision = resolve_provisioning_decision(
            declared=declared,
            importable=importable,
            runtime_kind=runtime_kind,
            enabled=enabled,
            subagent_depth=subagent_depth,
            already_attempted=(
                provisioning_already_attempted(declared.package) if declared is not None else False
            ),
        )
        if decision.action is ProvisioningAction.SKIP:
            return
        if decision.action is ProvisioningAction.REPORT_GAP:
            store.append(
                "env_gap_detected",
                {
                    "package": decision.package,
                    "trigger_config_file": decision.trigger_config_file,
                    "runtime_kind": runtime_kind.value,
                    "reason": decision.reason,
                },
            )
            return
        outcome = provision_test_runner(
            decision,
            run_command=_shell_command_probe(shell_runner=shell_runner, root=root),
        )
        if outcome is None:
            # Another session in this process claimed the one attempt; nothing
            # happened here, so nothing is reported here.
            return
        store.append(
            "env_provisioned",
            {**outcome.payload(), "runtime_kind": runtime_kind.value},
        )
    except Exception as exc:  # noqa: BLE001 - provisioning must never break startup
        try:
            store.append(
                "warning",
                {"warning": "workspace_provisioning_failed", "error": str(exc)},
            )
        except Exception:  # noqa: BLE001 - best-effort diagnostic
            pass


def create_session(
    *,
    cfg: AppConfig,
    root: Path,
    mode: str,
    yes: bool,
    max_steps: int | None,
    no_log: bool,
    api_key_override: str | None = None,
    console: Any | None = None,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    persona_allow_write_globs: list[str] | None = None,
    non_interactive: bool = False,
    one_shot_execution: bool = False,
    enable_chat_turn_step_budget: bool = False,
    chat_turn_fixed_override: int | None = None,
    session_log_dir_override: Path | None = None,
    session_id_override: str | None = None,
    prompt_cache_parent_session_id: str | None = None,
    surface: Surface | None = None,
    usage_role: str = "main",
    trusted_system_prompt_override: str | None = None,
    trusted_system_prompt_append: str | None = None,
    untrusted_prompt_prelude: str | None = None,
    enable_compaction: bool = True,
    enable_tool_output_offload: bool | None = None,
    enable_conversation_summarization: bool | None = None,
    compaction_profile: str = "chat",
    verification_enabled: bool = True,
    authoritative_verification_commands: list[str] | None = None,
    verify_cmd: list[str] | None = None,
    subagents_enabled: bool | None = None,
    helper_subagents_enabled: bool = False,
    enforce_explicit_subagent_requests: bool = True,
    subagent_depth: int = 0,
    subagent_registry: dict[str, SubagentDefinition] | None = None,
    workspace_binding: WorkspaceBinding | None = None,
    active_workdir_relpath_override: str | None = None,
    runtime_kind: RuntimeKind | str | None = None,
    mcp_manager: McpManager | ForgeTaskScopedMcpManager | None = None,
    session_source: str = "startup",
    session_source_metadata: dict[str, Any] | None = None,
    execution_deadline: ExecutionDeadline | None = None,
    crash_diagnostic_log_path: str | Path | None = None,
    crash_diagnostic_logger: CrashDiagnosticLogger | None = None,
    tool_dispatch_guard: ToolDispatchGuard | None = None,
    managed_browser_service: ManagedBrowserService | None = None,
    managed_browser_owner_id: str | None = None,
    managed_browser_cancel_check: Callable[[], bool] | None = None,
    host_action_handler: HostActionHandler | None = None,
    host_action_capabilities: Collection[str] | None = None,
    readonly_child_web_tool_names: Collection[str] | None = None,
    child_managed_browser_tool_names: Collection[str] | None = None,
) -> AgentSession:
    surface = surface or NoopSurface()
    resolved_runtime_kind = resolve_session_runtime_kind(
        runtime_kind=runtime_kind,
        one_shot_execution=one_shot_execution,
        subagent_depth=subagent_depth,
    )
    parent_steer_inbox = SteerInbox()
    workspace_trust_prompt = _build_workspace_trust_prompt(
        surface=surface,
        non_interactive=non_interactive,
    )
    prompt_context = prepare_session_prompt_context(
        cfg=cfg,
        root=root,
        mode=mode,
        yes=yes,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        persona_allow_write_globs=persona_allow_write_globs,
        non_interactive=non_interactive,
        one_shot_execution=one_shot_execution,
        verification_enabled=verification_enabled,
        authoritative_verification_commands=authoritative_verification_commands,
        verify_cmd=verify_cmd,
        trusted_system_prompt_override=trusted_system_prompt_override,
        trusted_system_prompt_append=trusted_system_prompt_append,
        untrusted_prompt_prelude=untrusted_prompt_prelude,
        subagents_enabled=subagents_enabled,
        subagent_depth=subagent_depth,
        subagent_registry=subagent_registry,
        workspace_binding=workspace_binding,
        workspace_trust_prompt=workspace_trust_prompt,
    )
    root = prompt_context.root
    workspace_context = prompt_context.workspace_context
    binding_requested_path = prompt_context.binding_requested_path
    binding_source = prompt_context.binding_source
    binding_risk_level = prompt_context.binding_risk_level
    binding_created_path = prompt_context.binding_created_path
    authoritative_verify_commands = prompt_context.authoritative_verify_commands
    session_cfg = prompt_context.session_cfg
    session_cfg.step_budget_policy = normalize_step_budget_policy(session_cfg.step_budget_policy)
    session_cfg, native_streaming_warning_message = _disable_unsupported_native_streaming(
        cfg=session_cfg
    )
    normalized_session_source = str(session_source or "startup").strip().lower() or "startup"
    if normalized_session_source not in {"startup", "resume", "fork"}:
        raise ConfigError("session_source must be one of: startup, resume, fork.")
    if session_source_metadata is not None and not isinstance(session_source_metadata, dict):
        raise ConfigError("session_source_metadata must be an object.")
    normalized_session_source_metadata = (
        copy.deepcopy(session_source_metadata) if session_source_metadata is not None else {}
    )
    if active_workdir_relpath_override is None:
        initial_active_workdir_relpath = _normalize_workspace_relpath(
            workspace_context.focus_relpath
        )
    else:
        initial_active_workdir_relpath = _normalize_workspace_relpath(
            active_workdir_relpath_override
        )
    initial_active_workdir = resolve_workdir_relpath_within_workspace(
        workspace_root=workspace_context.workspace_root,
        relpath=initial_active_workdir_relpath,
    )
    if not initial_active_workdir.exists():
        raise SessionWorkdirError(f"Directory does not exist: {initial_active_workdir}")
    if not initial_active_workdir.is_dir():
        raise SessionWorkdirError(f"Path is not a directory: {initial_active_workdir}")

    active_profile = get_active_profile(session_cfg)
    if api_key_override is None:
        if active_profile.auth_provider:
            api_key = ""
            api_key_source = f"provider-auth:{active_profile.auth_provider}"
        else:
            api_key_resolution = resolve_api_key(session_cfg)
            if api_key_resolution.key is None:
                api_key = get_api_key(session_cfg)
                api_key_source = "missing"
            else:
                api_key = api_key_resolution.key
                api_key_source = api_key_resolution.source
    else:
        api_key = api_key_override.strip()
        if not api_key:
            raise ConfigError("API key is empty.")
        api_key_source = "override"
    coding_temperature = resolve_role_temperature(session_cfg, role="coding")
    review_temperature = resolve_role_temperature(session_cfg, role="review")
    planner_temperature = resolve_role_temperature(session_cfg, role="planner")
    conflict_review_temperature = resolve_role_temperature(session_cfg, role="conflict_review")
    compactor_temperature = resolve_role_temperature(session_cfg, role="compactor")
    chat_temperature = resolve_role_temperature(session_cfg, role="chat")
    llm_timeout_s = resolve_llm_timeout_s(session_cfg)
    llm_enable_thinking = resolve_llm_enable_thinking(session_cfg)
    llm_reasoning_effort = resolve_llm_reasoning_effort(session_cfg)
    active_profile_name = active_profile.name
    active_profile_base_url = resolve_effective_base_url(
        cfg=session_cfg,
        profile=active_profile,
    )
    cache_affinity_preset = find_preset_for_profile(active_profile)
    session_scoped_cache_affinity = (
        cache_affinity_preset is not None
        and str(cache_affinity_preset.provider_key or "").strip().lower() == "moonshot"
    )

    def _prompt_cache_namespace(role: str) -> str | None:
        return build_prompt_cache_namespace(
            workspace_root=workspace_context.workspace_root,
            role=role,
            profile_name=active_profile_name,
            session_id=session_id if session_scoped_cache_affinity else None,
        )

    registry = ModelRegistry(cfg=session_cfg, api_key=api_key)
    # Deprecated no-op: recorded for observability and the (ignored)
    # AgentSession.routing_mode field; nothing consults it.
    routing_mode = str(getattr(session_cfg, "routing_mode", "auto") or "auto")
    resolved_subagents_enabled = prompt_context.resolved_subagents_enabled
    resolved_skills_enabled = prompt_context.resolved_skills_enabled
    skills_auto_invoke = prompt_context.skills_auto_invoke
    activation_decision = prompt_context.activation_decision
    plugin_activation_index = _build_plugin_activation_index(root)
    plugin_activation_dropped_counts: Counter[str] = Counter(
        prompt_context.plugin_activation_dropped_counts
    )
    discovered_skills = prompt_context.discovered_skills
    repo_conventions = prompt_context.repo_conventions
    effective_one_shot_execution = prompt_context.effective_one_shot_execution
    resolved_subagent_registry = prompt_context.resolved_subagent_registry
    step_budget_runtime = StepBudgetRuntime()

    session_id = session_id_override.strip() if session_id_override else make_session_id()
    if not session_id:
        session_id = make_session_id()
    prompt_cache_stream_key = resolve_prompt_cache_key(session_cfg)
    if prompt_cache_stream_key is None and session_cfg.cache.prompt_cache_key_enabled:
        prompt_cache_stream_key = derive_prompt_cache_stream_key(
            session_id=session_id,
            parent_session_id=prompt_cache_parent_session_id,
        )
    if mcp_manager is None:
        create_mcp_manager_fn = _patchable("create_mcp_manager", create_mcp_manager)
        if create_mcp_manager_fn is not _DEFAULT_CREATE_MCP_MANAGER:
            mcp_manager = create_mcp_manager_fn(
                workspace_root=workspace_context.workspace_root,
                runtime_kind=resolved_runtime_kind,
                session_id=session_id,
            )
        else:
            resolved_mcp_config = load_resolved_mcp_config(
                workspace_root=workspace_context.workspace_root
            )
            resolved_mcp_config, mcp_dropped_counts = _filter_mcp_config_for_plugins(
                config=resolved_mcp_config,
                activation_decision=activation_decision,
            )
            plugin_activation_dropped_counts.update(mcp_dropped_counts)
            mcp_manager = McpManager(
                resolved_config=resolved_mcp_config,
                workspace_root=workspace_context.workspace_root,
                runtime_kind=resolved_runtime_kind,
                session_id=session_id,
            )
    compaction_settings = resolve_compaction_settings(session_cfg)
    runtime_context_features = resolve_runtime_context_features(
        settings=compaction_settings,
        enable_compaction=enable_compaction,
        enable_tool_output_offload=enable_tool_output_offload,
        enable_conversation_summarization=enable_conversation_summarization,
        logging_enabled=not no_log,
        explicit_session_artifact_root=session_log_dir_override is not None,
    )
    compaction_enabled = runtime_context_features.any_enabled
    tool_output_offload_enabled = runtime_context_features.tool_output_offload_enabled
    conversation_summarization_enabled = runtime_context_features.conversation_summarization_enabled
    compactor_model_name: str | None = None
    if conversation_summarization_enabled:
        compactor_model_name = resolve_model_for_role(
            cfg=session_cfg,
            role=ROLE_COMPACTOR,
            plan=None,
        )
    active_model_refs = [ActiveModelRef(role=ROLE_CODING, model_name=session_cfg.model)]
    if compactor_model_name:
        active_model_refs.append(
            ActiveModelRef(role=ROLE_COMPACTOR, model_name=compactor_model_name)
        )
    model_metadata_policy_result = evaluate_active_model_metadata_policy(
        cfg=session_cfg,
        registry=registry,
        active_models=active_model_refs,
    )
    client = _make_session_llm_client(
        cfg=session_cfg,
        api_key=api_key,
        model=session_cfg.model,
        timeout_s=llm_timeout_s,
        temperature=coding_temperature,
        prompt_cache_key=prompt_cache_stream_key,
        prompt_cache_retention=resolve_prompt_cache_retention(session_cfg),
        prompt_cache_namespace=_prompt_cache_namespace(ROLE_CODING),
        enable_thinking=llm_enable_thinking,
        reasoning_effort=llm_reasoning_effort,
        session_id=session_id,
    )
    sessions_dir = (
        session_log_dir_override
        if session_log_dir_override is not None
        else resolve_sessions_dir(session_cfg)
    )
    store = SessionStore(
        enabled=not no_log,
        artifact_persistence_enabled=(not no_log) or session_log_dir_override is not None,
        sessions_dir=sessions_dir,
        session_id=session_id,
        cwd=str(initial_active_workdir),
        repo_root=str(root),
        workspace_root=str(workspace_context.workspace_root),
        focus_dir=str(workspace_context.focus_path),
        git_root=(
            str(workspace_context.git_root) if workspace_context.git_root is not None else None
        ),
        workspace_kind=workspace_context.workspace_kind,
        binding_source=binding_source,
        binding_requested_path=binding_requested_path,
        binding_risk_level=binding_risk_level,
        binding_created_path=binding_created_path,
        runtime_kind=resolved_runtime_kind.value,
        active_workdir=str(initial_active_workdir),
        active_workdir_relpath=initial_active_workdir_relpath,
    )
    resolved_crash_diagnostic_log_path = resolve_crash_diagnostic_log_path(
        session_cfg,
        cli_diagnostic_log_path=crash_diagnostic_log_path,
    )
    crash_diagnostics = crash_diagnostic_logger or build_crash_diagnostic_logger(
        path=resolved_crash_diagnostic_log_path,
        run_id=session_id,
        session_id=session_id,
        runtime_kind=resolved_runtime_kind.value,
    )
    crash_diagnostics.event(
        "run_started",
        {
            "runtime_kind": resolved_runtime_kind.value,
            "model": session_cfg.model,
            "max_steps": max_steps,
            "session_source": normalized_session_source,
            "deadline": (
                execution_deadline.telemetry_snapshot() if execution_deadline is not None else None
            ),
        },
        durable=True,
    )
    surface_on_warning = _meaningful_surface_warning_handler(surface)

    def _emit_hook_warning(message: str) -> None:
        clean = str(message or "").strip()
        if not clean:
            return
        store.append("warning", {"warning": "hook_warning", "message": clean})
        if callable(surface_on_warning):
            surface_on_warning(clean)
        else:
            warnings.warn(clean, stacklevel=2)

    tool_output_offloader: ToolOutputOffloader | None = None
    if tool_output_offload_enabled:
        workspace_artifacts_enabled = resolved_runtime_kind != RuntimeKind.SWARM_WORKER
        tool_output_offloader = ToolOutputOffloader(
            artifact_layout=store.session_artifact_layout,
            workspace_root=root,
            threshold_chars=compaction_settings.tool_output_offload_threshold_chars,
            preview_chars=compaction_settings.tool_output_preview_chars,
            workspace_artifacts_enabled=workspace_artifacts_enabled,
        )
    system_prompt = prompt_context.system_prompt
    system_prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    effective_deny_write_prefixes = prompt_context.effective_deny_write_prefixes
    effective_allow_write_globs = prompt_context.effective_allow_write_globs
    effective_verification_selection = prompt_context.effective_verification_selection
    effective_verification_commands = prompt_context.effective_verification_commands
    recommended_verification_commands = prompt_context.recommended_verification_commands
    verification_selection_metadata = verification_selection_payload(
        effective_verification_selection,
        authoritative=is_authoritative_verify_command_selection(effective_verification_selection),
    )

    usage_summary = UsageSummary()
    custom_tool_session_state = build_custom_tool_session_state(
        workspace_root=root,
        custom_tools_enabled=bool(getattr(session_cfg, "custom_tools_enabled", True)),
        mode=mode,
        runtime_kind=resolved_runtime_kind,
        built_in_tool_names={spec.name.casefold() for spec in iter_builtin_tool_metadata()},
        write_scope_restricted=_custom_tools_write_scope_restricted(
            mode=mode,
            deny_write_prefixes=deny_write_prefixes,
            allow_write_globs=allow_write_globs,
            persona_allow_write_globs=persona_allow_write_globs,
        ),
    )
    (
        custom_tool_session_state,
        custom_tool_dropped_counts,
    ) = _filter_custom_tool_session_state_for_plugins(
        state=custom_tool_session_state,
        activation_decision=activation_decision,
        index=plugin_activation_index,
    )
    plugin_activation_dropped_counts.update(custom_tool_dropped_counts)
    if activation_decision.untrusted_project_plugin_ids:
        ids = ", ".join(sorted(activation_decision.untrusted_project_plugin_ids))
        warning_message = f"Ignoring untrusted project plugin overrides: {ids}"
        store.append(
            "workspace_trust_untrusted_overrides",
            {"plugin_ids": sorted(activation_decision.untrusted_project_plugin_ids)},
        )
        if callable(surface_on_warning):
            surface_on_warning(warning_message)
        else:
            warnings.warn(warning_message, stacklevel=2)

    if native_streaming_warning_message:
        store.append(
            "warning",
            {
                "warning": "native_streaming_disabled",
                "message": native_streaming_warning_message,
            },
        )
        if callable(surface_on_warning):
            surface_on_warning(native_streaming_warning_message)
        else:
            warnings.warn(native_streaming_warning_message, stacklevel=2)

    for verification_warning_message in prompt_context.verification_selection_warnings:
        store.append(
            "warning",
            {
                "warning": "verification_selection_degraded",
                "message": verification_warning_message,
                **verification_selection_metadata,
            },
        )
        if callable(surface_on_warning):
            surface_on_warning(verification_warning_message)
        else:
            warnings.warn(verification_warning_message, stacklevel=2)

    # Determinism controls and the effective-configuration snapshot, both
    # resolved before the first provider call so nothing is recorded against a
    # configuration that was not yet in force.
    #
    # Only the top-level session does this. A subagent shares the process, so
    # it shares the installed sampling settings; re-resolving per subagent
    # would just rewrite the same values, and re-snapshotting would bury the
    # run's one authoritative config record under near-duplicates.
    if subagent_depth == 0:
        sampling_settings = resolve_sampling_settings(
            config_values=cfg.model_dump(),
        )
        set_active_sampling_settings(sampling_settings)
        for sampling_warning in sampling_settings.warnings:
            # An invalid sampling value is ignored rather than fatal (a typo in
            # a benchmark harness must not abort a campaign), so it has to be
            # visible in the log or it is indistinguishable from never having
            # been set at all.
            store.append(
                "warning",
                {
                    "warning": "sampling_setting_ignored",
                    "message": sampling_warning.message(),
                    **sampling_warning.payload(),
                },
            )
        store.append(
            CONFIG_SNAPSHOT_EVENT,
            config_snapshot_payload(
                config_values=cfg.model_dump(),
                version=__version__,
                build_info=load_build_info().telemetry_payload(),
                sampling=sampling_settings,
            ),
        )

    store.append(
        "session_start",
        {
            "session_source": normalized_session_source,
            "session_source_metadata": normalized_session_source_metadata,
            "mode": mode,
            "runtime_kind": resolved_runtime_kind.value,
            "max_steps": max_steps,
            "step_budget_policy": session_cfg.step_budget_policy,
            "task_max_steps": session_cfg.task_max_steps,
            "subagent_max_steps": session_cfg.subagent_max_steps,
            "model": session_cfg.model,
            "router_model": "",
            "base_url_descriptor": endpoint_descriptor(session_cfg.base_url),
            "profile_name": active_profile.name,
            "protocol": active_profile.protocol,
            "provider_base_url_descriptor": endpoint_descriptor(active_profile_base_url),
            "auth_provider": active_profile.auth_provider,
            "reasoning_trace_adapter": active_profile.reasoning_trace_adapter,
            "api_key_source": api_key_source,
            "temperature": session_cfg.temperature,
            "coding_temperature": coding_temperature,
            "review_temperature": review_temperature,
            "planner_temperature": planner_temperature,
            "conflict_review_temperature": conflict_review_temperature,
            "compactor_temperature": compactor_temperature,
            "chat_temperature": chat_temperature,
            "llm_enable_thinking": llm_enable_thinking,
            "llm_reasoning_effort": llm_reasoning_effort,
            "stream": session_cfg.stream,
            "routing_mode": routing_mode,
            "subagents_enabled": resolved_subagents_enabled,
            "skills_enabled": resolved_skills_enabled,
            "skills_auto_invoke": skills_auto_invoke,
            "custom_tools_enabled": bool(getattr(session_cfg, "custom_tools_enabled", True)),
            "custom_tool_count": len(custom_tool_session_state.discovery.effective_tools),
            "custom_tool_issue_count": len(custom_tool_session_state.discovery.issues),
            "discovered_skill_count": len(discovered_skills.ordered),
            "repo_convention_count": len(repo_conventions),
            "skill_discovery_issues": [
                {
                    "source_path": issue.source_path.as_posix(),
                    "message": issue.message,
                }
                for issue in discovered_skills.issues
            ],
            "subagent_depth": subagent_depth,
            "subagent_count": len(resolved_subagent_registry),
            "root": str(root),
            "workspace_root": str(workspace_context.workspace_root),
            "focus_dir": str(workspace_context.focus_path),
            "focus_relpath": workspace_context.focus_relpath,
            "active_workdir": str(initial_active_workdir),
            "active_workdir_relpath": initial_active_workdir_relpath,
            "workspace_kind": workspace_context.workspace_kind,
            "git_root": (
                str(workspace_context.git_root) if workspace_context.git_root is not None else None
            ),
            "has_head_commit": workspace_context.has_head_commit,
            "current_branch": workspace_context.current_branch,
            "binding_requested_path": binding_requested_path,
            "binding_source": binding_source,
            "binding_risk_level": binding_risk_level,
            "binding_created_path": binding_created_path,
            "usage_role": usage_role,
            "yes": yes,
            "non_interactive": non_interactive,
            "one_shot_execution": effective_one_shot_execution,
            "enable_chat_turn_step_budget": enable_chat_turn_step_budget,
            "workspace_grounding": prompt_context.workspace_grounding.to_payload(),
            "chat_turn_fixed_override": chat_turn_fixed_override,
            "verification_enabled": verification_enabled,
            "effective_verification_commands": effective_verification_commands,
            **verification_selection_metadata,
            "model_metadata_policy": model_metadata_policy_result.policy,
            "model_metadata_diagnostics": [
                diagnostic.as_payload() for diagnostic in model_metadata_policy_result.diagnostics
            ],
            "deny_write_prefixes": effective_deny_write_prefixes,
            "allow_write_globs": effective_allow_write_globs,
            "recommended_verification_commands": recommended_verification_commands,
            "authoritative_verification_commands": authoritative_verify_commands,
            "system_prompt_sha256": system_prompt_sha256,
            "requested_enable_compaction": runtime_context_features.requested_enable_compaction,
            "requested_tool_output_offload": (
                runtime_context_features.requested_tool_output_offload
            ),
            "requested_conversation_summarization": (
                runtime_context_features.requested_conversation_summarization
            ),
            "logging_enabled": runtime_context_features.logging_enabled,
            "explicit_session_artifact_root": (
                runtime_context_features.explicit_session_artifact_root
            ),
            "tool_output_offload_artifact_persistence_available": (
                runtime_context_features.tool_output_offload_artifact_persistence_available
            ),
            "compaction_enabled": compaction_enabled,
            "compaction_settings_enabled": runtime_context_features.settings_enabled,
            "tool_output_offload_enabled": tool_output_offload_enabled,
            "compaction_settings_offload_tool_outputs": (
                runtime_context_features.settings_offload_tool_outputs
            ),
            "tool_output_offload_threshold_chars": (
                compaction_settings.tool_output_offload_threshold_chars
            ),
            "tool_output_preview_chars": compaction_settings.tool_output_preview_chars,
            "conversation_summarization_enabled": conversation_summarization_enabled,
            "compaction_profile": compaction_profile,
            "compaction_settings_summarize_conversation": (
                runtime_context_features.settings_summarize_conversation
            ),
            "compaction_recent_user_turns_to_keep": (compaction_settings.recent_user_turns_to_keep),
            "compaction_trigger_ratio": compaction_settings.trigger_ratio,
            "compaction_target_ratio": compaction_settings.target_ratio,
            "compaction_max_chunk_messages": compaction_settings.max_chunk_messages,
            "compaction_safety_margin_tokens": compaction_settings.safety_margin_tokens,
            "compactor_model": compactor_model_name,
            "mcp": mcp_manager.startup_metadata(),
        },
    )

    if callable(surface_on_warning):
        for warning_message in model_metadata_policy_result.warning_messages:
            surface_on_warning(warning_message)
    else:
        for warning_message in model_metadata_policy_result.warning_messages:
            warnings.warn(warning_message, stacklevel=2)

    if conversation_summarization_enabled and compactor_model_name == session_cfg.model:
        store.append(
            "warning",
            {
                "warning": "compactor_model_equals_main_model",
                "model": session_cfg.model,
            },
        )
    for issue in discovered_skills.issues:
        store.append(
            "warning",
            {
                "warning": "skill_discovery_issue",
                "source_path": issue.source_path.as_posix(),
                "message": issue.message,
            },
        )
    for issue in custom_tool_session_state.discovery.issues:
        store.append(
            "warning",
            {
                "warning": "custom_tool_discovery_issue",
                "source_scope": issue.source_scope,
                "source_path": issue.source_path.as_posix(),
                "tool_name": issue.tool_name,
                "code": issue.code,
                "message": issue.message,
            },
        )

    resolved_sandbox_settings = resolve_shell_sandbox_settings(session_cfg)
    process_group_registry = ProcessGroupRegistry()

    def _sandbox_warning_callback(message: str) -> None:
        store.append("sandbox_warning", {"message": message})

    def _with_process_group_registry(built_runner: Any) -> Any:
        # Attach the session's registry to whichever runner the (possibly patched)
        # builder produced. Backends that cannot be reaped by process group -- the
        # Docker runner tears its container down through the docker CLI instead --
        # simply do not declare the field and are left untouched.
        if not dataclasses.is_dataclass(built_runner):
            return built_runner
        field_names = {f.name for f in dataclasses.fields(built_runner)}
        if "process_group_registry" not in field_names:
            return built_runner
        return dataclasses.replace(built_runner, process_group_registry=process_group_registry)

    def _load_shell_runner_from_resolved_settings() -> Any:
        # stdin is closed for every foreground command, not just verification:
        # nobody is watching the terminal, so a tool that stops to ask a
        # question (ssh-keygen's "Overwrite (y/n)?", apt's config prompts) would
        # otherwise hold the pipe open until the hard kill.
        patched_build_shell_runner = _patchable("build_shell_runner", build_shell_runner)
        if patched_build_shell_runner is not build_shell_runner:
            return with_closed_stdin(
                _with_process_group_registry(
                    patched_build_shell_runner(
                        cfg=session_cfg,
                        root=root,
                        warning_callback=_sandbox_warning_callback,
                    )
                )
            )
        return with_closed_stdin(
            _with_process_group_registry(
                _patchable(
                    "build_shell_runner_from_settings",
                    build_shell_runner_from_settings,
                )(
                    resolved_sandbox_settings,
                    root,
                    warning_callback=_sandbox_warning_callback,
                )
            )
        )

    if mode == "readonly":
        runner = DisabledShellRunner(reason="shell_run is disabled in readonly mode.")
        bg_runner = DisabledBackgroundRunner(
            reason="Background shell tools are disabled in readonly mode."
        )
    else:
        runner = LazyShellRunner(_load_shell_runner_from_resolved_settings)
        bg_runner = LazyBackgroundShellRunner(
            lambda: _patchable(
                "build_background_shell_runner_from_settings",
                build_background_shell_runner_from_settings,
            )(
                resolved_sandbox_settings,
                root,
                warning_callback=_sandbox_warning_callback,
            )
        )

    # Warmup: close a declared-but-missing test-runner gap before the model
    # discovers it by failing a test run. Placed here because it must go through
    # the session's own shell runner -- the environment the agent's commands
    # resolve, and the one the operator's sandbox policy governs.
    _pre_provision_declared_test_runner(
        store=store,
        root=workspace_context.workspace_root,
        cfg=session_cfg,
        runtime_kind=resolved_runtime_kind,
        subagent_depth=subagent_depth,
        shell_runner=runner,
    )

    terminal_manager = TerminalManager(
        runner=bg_runner,
        settings=resolved_sandbox_settings,
    )
    # Session-scoped: only the persist starts this run made, so the
    # finalization re-check cannot report a neighbouring session's service.
    persistent_service_registry = PersistentServiceRegistry()
    # Session-scoped for the same reason, and shared between the tool layer
    # (which scores overwrites) and the turn controller (which counts repeated
    # failures and reports scratch files) so both halves see one run's history.
    edit_discipline_state = EditDisciplineState()
    durable_service_manager = DurableServiceManager(
        root=root,
        state_dir=store.sessions_dir / "durable_services",
        settings=resolved_sandbox_settings,
    )

    try:
        active_workdir_state: dict[str, Any] = {
            "relpath": initial_active_workdir_relpath,
            "session": None,
        }

        def _get_active_workdir_relpath() -> str:
            session_obj = active_workdir_state.get("session")
            if session_obj is not None:
                return resolve_session_active_workdir_relpath(session_obj)
            return _normalize_workspace_relpath(active_workdir_state.get("relpath"))

        def _set_active_workdir(raw_path: str, source: str) -> dict[str, Any]:
            session_obj = active_workdir_state.get("session")
            if session_obj is None:
                workspace_root = workspace_context.workspace_root.resolve()
                current_relpath = _normalize_workspace_relpath(active_workdir_state.get("relpath"))
                current_path = resolve_workdir_relpath_within_workspace(
                    workspace_root=workspace_root,
                    relpath=current_relpath,
                )
                next_path = _resolve_requested_workdir_within_workspace(
                    workspace_root=workspace_root,
                    current_workdir=current_path,
                    requested_path=raw_path,
                )
                next_relpath = _workspace_relpath_for_path(
                    workspace_root=workspace_root,
                    path=next_path,
                )
                changed = next_relpath != current_relpath
                active_workdir_state["relpath"] = next_relpath
                store.update_active_workdir(
                    cwd=os.fspath(next_path),
                    active_workdir_relpath=next_relpath,
                )
                payload = {
                    "source": source,
                    "workspace_root": os.fspath(workspace_root),
                    "focus_dir": os.fspath(workspace_context.focus_path),
                    "focus_relpath": workspace_context.focus_relpath,
                    "previous_active_workdir": os.fspath(current_path),
                    "previous_active_workdir_relpath": current_relpath,
                    "active_workdir": os.fspath(next_path),
                    "active_workdir_relpath": next_relpath,
                    "changed": changed,
                }
                if payload["changed"]:
                    store.append("session_workdir_changed", payload)
                return payload
            return set_session_active_workdir(session_obj, raw_path, source=source)

        def _get_verify_command_selection() -> ResolvedVerifyCommands | None:
            session_obj = active_workdir_state.get("session")
            if session_obj is not None:
                return _session_verify_command_selection(session_obj)
            return prompt_context.effective_verification_selection

        persona_switch_state = (
            PersonaSwitchState()
            if (
                resolved_runtime_kind == RuntimeKind.INTERACTIVE_CHAT
                and not non_interactive
                and persona_modes_enabled(session_cfg)
            )
            else None
        )
        persona_registry: dict[str, Any] | None = None
        persona_registry_warnings: tuple[str, ...] = ()
        if persona_switch_state is not None:
            try:
                loaded_personas, persona_registry_warnings = load_custom_personas(root)
                persona_registry = loaded_personas or None
            except Exception:  # noqa: BLE001 - custom personas must not break startup
                persona_registry = None
                persona_registry_warnings = ()
        completion_gate_tools_enabled = bool(
            subagent_depth == 0
            and str(mode or "").strip().lower() != "readonly"
            and (
                effective_one_shot_execution
                or (
                    resolved_runtime_kind == RuntimeKind.INTERACTIVE_CHAT
                    and enable_chat_turn_step_budget
                )
            )
        )
        child_scheduler_holder: dict[str, ChildScheduler] = {}
        read_ledger_holder: dict[str, SessionReadLedger] = {}

        def _capture_child_scheduler(scheduler: ChildScheduler) -> None:
            child_scheduler_holder["scheduler"] = scheduler

        def _capture_read_ledger(ledger: SessionReadLedger) -> None:
            read_ledger_holder["ledger"] = ledger

        tools = build_tools(
            root=root,
            console=console,
            surface=surface,
            persona_switch_state=persona_switch_state,
            store=store,
            process_group_registry=process_group_registry,
            mode=mode,
            yes=yes,
            cfg=session_cfg,
            api_key=api_key,
            max_steps=max_steps,
            no_log=no_log,
            usage_role=usage_role,
            usage_summary=usage_summary,
            model_registry=registry,
            deny_write_prefixes=deny_write_prefixes,
            allow_write_globs=allow_write_globs,
            persona_allow_write_globs=persona_allow_write_globs,
            non_interactive=non_interactive,
            shell_runner=runner,
            terminal_manager=terminal_manager,
            durable_service_manager=durable_service_manager,
            persistent_service_registry=persistent_service_registry,
            edit_discipline=edit_discipline_state,
            managed_browser_service=managed_browser_service,
            managed_browser_owner_id=managed_browser_owner_id,
            managed_browser_cancel_check=managed_browser_cancel_check,
            verification_enabled=verification_enabled,
            authoritative_verification_commands=authoritative_verify_commands,
            effective_verification_commands=effective_verification_commands,
            verify_command_selection=prompt_context.effective_verification_selection,
            get_verify_command_selection=_get_verify_command_selection,
            one_shot_execution=effective_one_shot_execution,
            completion_gate_tools_enabled=completion_gate_tools_enabled,
            skills_enabled=resolved_skills_enabled,
            skill_registry=discovered_skills.skills,
            subagents_enabled=resolved_subagents_enabled,
            helper_subagents_enabled=helper_subagents_enabled,
            subagent_depth=subagent_depth,
            subagent_registry=resolved_subagent_registry,
            session_log_dir_override=session_log_dir_override,
            step_budget_runtime=step_budget_runtime,
            emit_web_search_runtime_diagnostics=(subagent_depth == 0),
            runtime_kind=resolved_runtime_kind,
            mcp_manager=mcp_manager,
            custom_tool_session_state=custom_tool_session_state,
            get_active_workdir_relpath=_get_active_workdir_relpath,
            set_active_workdir_callback=_set_active_workdir,
            create_session_factory=create_session,
            prompt_cache_parent_session_id=(prompt_cache_parent_session_id or session_id),
            execution_deadline=execution_deadline,
            crash_diagnostic_log_path=resolved_crash_diagnostic_log_path,
            crash_diagnostics=crash_diagnostics,
            tool_dispatch_guard=tool_dispatch_guard,
            host_action_handler=host_action_handler,
            host_action_capabilities=host_action_capabilities,
            child_scheduler_sink=_capture_child_scheduler,
            parent_steer_inbox=parent_steer_inbox,
            read_ledger_sink=_capture_read_ledger,
            readonly_child_web_tool_names=readonly_child_web_tool_names,
            child_managed_browser_tool_names=child_managed_browser_tool_names,
        )
        if mcp_manager is not None and mcp_manager.resolved_config.has_any_config:
            store.append("mcp_catalog_snapshot", mcp_manager.catalog_snapshot_metadata())
        tool_list = [t.as_openai_tool() for t in tools.values()]
        messages: list[dict[str, Any]] = list(prompt_context.messages)
        if active_workdir_relpath_override is not None:
            binding_context = _workspace_binding_context_message(
                workspace_root=workspace_context.workspace_root,
                focus_dir=workspace_context.focus_path,
                focus_relpath=workspace_context.focus_relpath,
                workspace_kind=workspace_context.workspace_kind,
                active_workdir=initial_active_workdir,
                active_workdir_relpath=initial_active_workdir_relpath,
                binding_requested_path=binding_requested_path,
                binding_source=binding_source,
                binding_risk_level=binding_risk_level,
                binding_created_path=binding_created_path,
            )
            for index, message in enumerate(messages):
                if str(message.get("role") or "") != "user":
                    continue
                if (
                    not str(message.get("content") or "")
                    .lstrip()
                    .startswith("<workspace_binding_context>")
                ):
                    continue
                messages[index] = {**message, "content": binding_context}
                break
        pinned_prefix_len = prompt_context.pinned_prefix_len
        if resolved_subagents_enabled and subagent_depth == 0:
            effective_subagent_context = _subagent_context_message(
                subagent_registry=resolved_subagent_registry,
                unavailable_subagents=unavailable_builtin_subagents(
                    registry=resolved_subagent_registry,
                    cfg=session_cfg,
                    available_tool_names=set(tools),
                ),
                max_background_children=(
                    session_cfg.subagent_orchestration.max_background_children
                ),
            )
            replaced_subagent_context = False
            for message_index, message in enumerate(messages):
                if str(message.get("role") or "") == "user" and "<subagent_context>" in str(
                    message.get("content") or ""
                ):
                    if effective_subagent_context is not None:
                        messages[message_index] = {
                            **message,
                            "content": effective_subagent_context,
                        }
                    replaced_subagent_context = True
                    break
            if effective_subagent_context is not None and not replaced_subagent_context:
                insert_at = next(
                    (
                        index
                        for index, message in enumerate(messages)
                        if "<environment_context>" in str(message.get("content") or "")
                    ),
                    len(messages),
                )
                messages.insert(
                    insert_at,
                    {"role": "user", "content": effective_subagent_context},
                )
                pinned_prefix_len += 1
        hooks_config = load_resolved_hooks_config(workspace_context.workspace_root)
        hooks_config, hook_dropped_counts = _filter_hooks_config_for_plugins(
            config=hooks_config,
            activation_decision=activation_decision,
            index=plugin_activation_index,
        )
        plugin_activation_dropped_counts.update(hook_dropped_counts)
        dropped_counts_payload = _merge_dropped_counts(plugin_activation_dropped_counts)
        if dropped_counts_payload:
            store.append(
                "plugin_activation_filter",
                {
                    "enabled_plugin_ids": sorted(activation_decision.enabled_plugin_ids),
                    "dropped_component_counts": dropped_counts_payload,
                },
            )
        hook_dispatcher: HookDispatcher | None = None
        if hooks_config.untrusted_project_paths:
            untrusted_paths = [os.fspath(path) for path in hooks_config.untrusted_project_paths]
            store.append("hook_config_untrusted", {"paths": untrusted_paths})
            for path_text in untrusted_paths:
                _emit_hook_warning(
                    "Ignoring untrusted project hooks config: "
                    f"{path_text}. Run `alysis hooks trust --path "
                    f"{os.fspath(workspace_context.workspace_root)}` to allow it."
                )
        if hooks_config.has_any_hooks:
            hook_audit_artifact = (
                store.runtime_artifact_path(*HOOK_AUDIT_ARTIFACT_PARTS) if store.enabled else None
            )
            hook_dispatcher = HookDispatcher(
                config=hooks_config,
                workspace_root=workspace_context.workspace_root,
                repo_root=root,
                session_id=session_id,
                mode=mode,
                runtime_kind=resolved_runtime_kind.value,
                warning_callback=_emit_hook_warning,
                log_callback=store.append,
                audit_callback=(
                    (
                        lambda payload: store.append_artifact_jsonl(
                            *HOOK_AUDIT_ARTIFACT_PARTS, payload=payload
                        )
                    )
                    if store.enabled
                    else None
                ),
            )
            store.append(
                "hook_config_loaded",
                {
                    "loaded_paths": [os.fspath(path) for path in hooks_config.loaded_paths],
                    "events": {
                        event_name: len(groups)
                        for event_name, groups in hooks_config.groups_by_event.items()
                    },
                    "hook_audit_artifact": os.fspath(hook_audit_artifact)
                    if hook_audit_artifact is not None
                    else None,
                },
            )
            try:
                session_start_hook_result = hook_dispatcher.fire_session_start(
                    cwd=initial_active_workdir,
                    active_workdir_relpath=initial_active_workdir_relpath,
                    session_source=normalized_session_source,
                    payload={
                        "session_source": normalized_session_source,
                        "session_source_metadata": copy.deepcopy(
                            normalized_session_source_metadata
                        ),
                        "mode": mode,
                        "runtime_kind": resolved_runtime_kind.value,
                        "workspace_root": os.fspath(workspace_context.workspace_root),
                        "focus_dir": os.fspath(workspace_context.focus_path),
                        "focus_relpath": workspace_context.focus_relpath,
                        "active_workdir": os.fspath(initial_active_workdir),
                        "active_workdir_relpath": initial_active_workdir_relpath,
                        "workspace_kind": workspace_context.workspace_kind,
                        "current_branch": workspace_context.current_branch,
                        "max_steps": max_steps,
                        "non_interactive": non_interactive,
                        "one_shot_execution": effective_one_shot_execution,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _emit_hook_warning(f"Lifecycle hook dispatch failed: {exc}")
                session_start_hook_result = HookDispatchResult()
            for notice in session_start_hook_result.system_notices:
                _emit_hook_warning(notice)
            if session_start_hook_result.blocked:
                blocked_reason = session_start_hook_result.reason or "session start blocked by hook"
                raise ConfigError(f"Session blocked by hook: {blocked_reason}")
            for hook_message in session_start_hook_result.additional_system_messages:
                messages.append({"role": "system", "content": hook_message})
                pinned_prefix_len += 1
                store.append(
                    "hook_message_added",
                    {
                        "event_name": "SessionStart",
                        "role": "system",
                        "chars": len(hook_message),
                        "pinned": True,
                    },
                )
            for hook_message in session_start_hook_result.additional_user_messages:
                messages.append({"role": "user", "content": hook_message})
                pinned_prefix_len += 1
                store.append(
                    "hook_message_added",
                    {
                        "event_name": "SessionStart",
                        "role": "user",
                        "chars": len(hook_message),
                        "pinned": True,
                    },
                )

        startup_messages = copy.deepcopy(messages)

        conversation_compactor: ConversationCompactor | None = None
        if conversation_summarization_enabled and compactor_model_name:
            compactor_client = _make_session_llm_client(
                cfg=session_cfg,
                api_key=api_key,
                model=compactor_model_name,
                timeout_s=llm_timeout_s,
                temperature=compactor_temperature,
                prompt_cache_key=prompt_cache_stream_key,
                prompt_cache_retention=resolve_prompt_cache_retention(session_cfg),
                prompt_cache_namespace=_prompt_cache_namespace(ROLE_COMPACTOR),
                enable_thinking=llm_enable_thinking,
                reasoning_effort=llm_reasoning_effort,
                session_id=session_id,
            )
            conversation_compactor = ConversationCompactor(
                root=root,
                artifact_layout=store.session_artifact_layout,
                store=store,
                settings=compaction_settings,
                compactor_client=compactor_client,
                model_registry=registry,
                usage_summary=usage_summary,
                usage_role=usage_role,
                pinned_prefix_len=pinned_prefix_len,
                profile=("execution" if compaction_profile == "execution" else "chat"),
                input_token_counter=(
                    lambda count_messages, count_tools: count_input_tokens_if_supported(
                        client=client,
                        messages=count_messages,
                        tools=effective_tools_for_client(client, count_tools),
                    )
                ),
                calibration_filters=usage_context_from_client_response(
                    client=client,
                    response=None,
                    operation="main_llm",
                ),
            )

        if _surface_needs_startup_git_status(surface):
            git_branch = _patchable("_git_branch", _git_branch)
            git_is_dirty = _patchable("_git_is_dirty", _git_is_dirty)
            startup_branch = git_branch(root)
            startup_dirty = git_is_dirty(root)
        else:
            startup_branch = "-"
            startup_dirty = False

        startup_context_baseline_tokens = estimate_request_token_breakdown(
            messages=messages,
            tool_list=effective_tools_for_client(client, tool_list),
            pinned_prefix_len=pinned_prefix_len,
        ).total_tokens

        surface.on_status_update(
            StatusEvent(
                mode=mode,
                model=session_cfg.model,
                workspace=os.fspath(root),
                session_id=session_id,
                branch=startup_branch,
                dirty=startup_dirty,
                stream=session_cfg.stream,
                task="-",
            )
        )

        session = AgentSession(
            cfg=session_cfg,
            root=root,
            mode=mode,
            persona=(
                normalize_persona(getattr(session_cfg, "default_persona", "code"), persona_registry)
                if persona_modes_enabled(session_cfg)
                else "code"
            ),
            persona_switch_state=persona_switch_state,
            persona_registry=persona_registry,
            persona_registry_warnings=persona_registry_warnings,
            yes=yes,
            stream=session_cfg.stream,
            routing_mode=routing_mode,
            max_steps=max_steps,
            api_key=api_key,
            api_key_source=api_key_source,
            no_log=no_log,
            non_interactive=non_interactive,
            one_shot_execution=effective_one_shot_execution,
            enable_chat_turn_step_budget=enable_chat_turn_step_budget,
            chat_turn_fixed_override=chat_turn_fixed_override,
            verification_enabled=verification_enabled,
            effective_verification_commands=list(effective_verification_commands),
            authoritative_verification_commands=(
                list(authoritative_verify_commands)
                if authoritative_verify_commands is not None
                else None
            ),
            verification_selection_source=str(
                verification_selection_metadata.get("verification_selection_source") or ""
            ),
            verification_selection_reason=str(
                verification_selection_metadata.get("verification_selection_reason") or ""
            ),
            verification_contract_type=str(
                verification_selection_metadata.get("verification_contract_type") or ""
            ),
            verification_authoritative=bool(
                verification_selection_metadata.get("verification_authoritative", False)
            ),
            verification_best_effort=bool(
                verification_selection_metadata.get("verification_best_effort", False)
            ),
            deny_write_prefixes=(
                list(deny_write_prefixes) if deny_write_prefixes is not None else None
            ),
            allow_write_globs=(list(allow_write_globs) if allow_write_globs is not None else None),
            persona_allow_write_globs=(
                list(persona_allow_write_globs) if persona_allow_write_globs is not None else None
            ),
            session_log_dir_override=session_log_dir_override,
            skills_enabled=resolved_skills_enabled,
            skills_auto_invoke=skills_auto_invoke,
            skill_registry=dict(discovered_skills.skills),
            skills_ordered=tuple(discovered_skills.ordered),
            skill_discovery_issues=tuple(discovered_skills.issues),
            skill_catalog_entries=tuple(prompt_context.skill_catalog_entries),
            repo_conventions=tuple(repo_conventions),
            console=console,
            surface=surface,
            store=store,
            client=client,
            persona_client_cache={(session_cfg.model, coding_temperature): client},
            persona_client_key=(session_cfg.model, coding_temperature),
            model_registry=registry,
            usage_summary=usage_summary,
            usage_role=usage_role,
            tool_output_offloader=tool_output_offloader,
            conversation_compactor=conversation_compactor,
            tool_output_offload_enabled=tool_output_offload_enabled,
            conversation_summarization_enabled=conversation_summarization_enabled,
            compaction_profile=compaction_profile,
            tools=tools,
            tool_list=tool_list,
            messages=messages,
            startup_messages=startup_messages,
            runtime_kind=resolved_runtime_kind,
            prompt_cache_stream_key=prompt_cache_stream_key,
            mcp_manager=mcp_manager,
            terminal_manager=terminal_manager,
            durable_service_manager=durable_service_manager,
            persistent_service_registry=persistent_service_registry,
            edit_discipline=edit_discipline_state,
            subagents_enabled=resolved_subagents_enabled,
            enforce_explicit_subagent_requests=bool(enforce_explicit_subagent_requests),
            subagent_depth=subagent_depth,
            subagent_registry=resolved_subagent_registry,
            child_scheduler=child_scheduler_holder.get("scheduler"),
            steer_inbox=parent_steer_inbox,
            read_ledger=read_ledger_holder.get("ledger"),
            shell_runner=runner,
            step_budget_runtime=step_budget_runtime,
            planner_workspace_context=prompt_context.planner_workspace_context,
            workspace_grounding=prompt_context.workspace_grounding,
            focus_dir=workspace_context.focus_path,
            focus_relpath=workspace_context.focus_relpath,
            workspace_kind=workspace_context.workspace_kind,
            binding_requested_path=binding_requested_path,
            binding_source=binding_source,
            binding_risk_level=binding_risk_level,
            binding_created_path=binding_created_path,
            active_workdir_relpath=initial_active_workdir_relpath,
            session_source=normalized_session_source,
            session_source_metadata=copy.deepcopy(normalized_session_source_metadata),
            pinned_prefix_len=pinned_prefix_len,
            startup_context_baseline_tokens=startup_context_baseline_tokens,
            custom_tool_session_state=custom_tool_session_state,
            hook_dispatcher=hook_dispatcher,
            execution_deadline=execution_deadline,
            crash_diagnostics=crash_diagnostics,
            crash_diagnostic_log_path=resolved_crash_diagnostic_log_path,
            agentbox_telemetry=AgentBoxTelemetry.from_env(
                root=root,
                runtime_version=f"alysis-{__version__}",
            ),
            process_group_registry=process_group_registry,
        )
        if subagent_depth == 0 and store.enabled:
            # Persist the process's provider/web-search telemetry to the run's artifact
            # dir so retry/throttle/latency history survives exit. Only the top-level
            # session registers the sink; nested subagent/candidate calls in the same
            # process flow into it. Released on close().
            set_provider_telemetry_sink(
                store.runtime_artifact_path("diagnostics", "provider_telemetry.jsonl")
            )
        active_workdir_state["session"] = session
        return session
    except Exception:
        try:
            terminal_manager.shutdown_all()
        except Exception as exc:  # noqa: BLE001
            # Startup failure cleanup is best-effort; still close MCP/store below.
            store.append(
                "warning",
                {
                    "warning": "terminal_shutdown_failed",
                    "message": f"Terminal manager shutdown failed: {exc}",
                },
            )
        try:
            mcp_manager.close()
        finally:
            store.close()
        raise
