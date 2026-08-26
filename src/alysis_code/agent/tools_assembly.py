from __future__ import annotations

import copy
import importlib
import inspect
import ipaddress
import json
import os
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from ..approval_scope import (
    exact_command_scope,
    exact_file_set_scope,
    exact_verify_command_set_scope,
)
from ..config import (
    AppConfig,
    ConfigError,
    resolve_web_search_policy,
    resolve_web_tools_enabled,
)
from ..context.tool_schema_budgeter import (
    CUSTOM_MCP_SCHEMA_FAMILIES,
    DEFAULT_CUSTOM_MCP_DESCRIPTION_MAX_CHARS,
    compact_custom_mcp_tool_parameters,
)
from ..crash_diagnostics import CrashDiagnosticLogger
from ..custom_tools import (
    CustomToolDiscoveryResult,
    CustomToolSessionState,
    CustomToolSpec,
    build_custom_tool_session_state,
    run_custom_tool,
)
from ..diff_paths import iter_patch_paths
from ..dispatch_timing import (
    DISPATCH_OVERHEAD_OPERATION,
    DispatchOverheadAccount,
    run_cancellable_wait,
)
from ..durable_service_manager import DurableServiceManager, ProcessOwnership
from ..edit_discipline import EditDisciplineState
from ..execution_deadline import (
    DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS,
    MINIMUM_TOOL_START_SECONDS,
    DeadlineExhausted,
    DeadlineOperation,
    DeadlinePhase,
    ExecutionDeadline,
    deadline_timeout_or_raise,
)
from ..extensions.activation import ActivationDecision
from ..extensions.models import normalize_extension_id
from ..host_actions import (
    HOST_ACTION_TOOL_NAMES,
    HostActionError,
    HostActionHandler,
    normalize_host_action_arguments,
    normalized_host_action_capabilities,
)
from ..ide.managed_browser import (
    BrowserArtifact,
    BrowserError,
    BrowserSessionStatus,
    ManagedBrowserService,
)
from ..ide.protocol import redact_secrets
from ..mcp.manager import ForgeTaskScopedMcpManager, McpManager
from ..mcp.models import ResolvedMcpConfig, ResolvedMcpServer
from ..model_registry import ModelRegistry
from ..personas import is_persona_name, persona_modes_enabled
from ..pipeline_facts import resolve_pipeline_stage_status
from ..policy import evaluate_shell_command
from ..process_reaping import ProcessGroupRegistry
from ..runtime_kind import RuntimeKind, normalize_runtime_kind
from ..service_persistence import (
    PersistentServiceRecord,
    PersistentServiceRegistry,
    check_service,
    readiness_spec_for_port,
    resolve_probe_port,
)
from ..session_store import SessionStore
from ..skills import SkillBundle, SkillReadError, read_skill_bundle_file, resolve_skill_by_name
from ..subagents import (
    EDIT_CAPABLE_SUBAGENT_TOOL_NAMES,
    SubagentDefinition,
    helper_subagent_names,
    routable_subagent_names,
)
from ..surface import (
    ApprovalRequest,
    NoopSurface,
    PatchEvent,
)
from ..surface.base import Surface
from ..task_scope import (
    ancestor_directory_scope_patterns,
    is_non_material_untracked_path,
    scope_path_matches_pattern,
)
from ..terminal_manager import ProcessOutputSnapshot, TerminalLimitError, TerminalManager
from ..tools.artifacts import SessionArtifactReadError, session_artifact_read
from ..tools.availability import mark_available, mark_unavailable, register_tool_availability
from ..tools.fs import (
    FsError,
    StaleFileError,
    assert_file_precondition,
    capture_file_precondition,
    classify_sensitive_path,
    fs_copy,
    fs_delete,
    fs_list,
    fs_mkdir,
    fs_move,
    fs_read,
    fs_read_lines,
    prepare_fs_edit,
    prepare_fs_write,
    write_prepared_fs_edit,
    write_prepared_fs_write,
)
from ..tools.git import git_apply_patch, git_diff, git_history, git_status
from ..tools.history import history_search
from ..tools.image_generation import (
    ImageGenerationError,
    generate_images,
    plan_image_output_paths,
)
from ..tools.registry import (
    REPORT_BLOCKER_MAX_MESSAGE_CHARS,
    built_in_subagent_tool_names,
    copied_tool_parameters,
    iter_builtin_tool_metadata,
    require_builtin_tool_metadata,
)
from ..tools.repo_map import repo_map
from ..tools.search import search_rg
from ..tools.shell import shell_run
from ..tools.symbols import symbol_search
from ..tools.test_discovery import test_discover
from ..tools.web import web_fetch
from ..tools.web_search import WebSearchError, resolve_web_search_runtime_status, web_search
from ..usage_tracker import UsageSummary
from ..verification_command_analysis import (
    VerificationCommandEvidentiaryCapability,
    analyze_verification_command,
)
from ..verify_gate import (
    ResolvedVerifyCommands,
    VerifyError,
    is_authoritative_verify_command_selection,
    resolve_verify_commands,
    run_task_verification,
    trusted_shell_expression_command_set,
    validation_errors_for_selection,
    verification_command_specs_payload,
    verification_selection_payload,
    verify_run_result_to_payload,
)
from ..web_research import (
    build_web_fetch_recovery_search_query,
    canonicalize_web_url_input,
    normalize_web_url,
)
from ..workspace_context import resolve_workspace_context
from . import _patchable
from .errors import AgentRuntimeError, ApprovalDeclinedError, SessionWorkdirError
from .mutation_classification import classify_mutation_paths
from .prompt_context import (
    _MODE_FULLACCESS,
    ALWAYS_PROTECTED_WRITE_PREFIXES,
    _component_plugin_allowed,
    _normalize_rel_match_path,
    _normalize_workspace_relpath,
    _normalized_authoritative_verify_commands,
    _normalized_verify_commands,
    _paths_require_verification,
    _PluginActivationIndex,
    _workspace_relpath_for_path,
    resolve_workdir_relpath_within_workspace,
)
from .read_ledger import SessionReadLedger
from .steering import SteerInbox
from .subagent_execution import (
    _AUTHORITATIVE_SUBAGENT_FINAL_TEXT_SOURCES as _AUTHORITATIVE_SUBAGENT_FINAL_TEXT_SOURCES,
)
from .subagent_execution import (
    _MODE_PERMISSIVENESS_ORDER as _MODE_PERMISSIVENESS_ORDER,
)
from .subagent_execution import (
    _MODE_PERMISSIVENESS_RANK as _MODE_PERMISSIVENESS_RANK,
)
from .subagent_execution import (
    _ROUTING_MODE_CODE_ONLY as _ROUTING_MODE_CODE_ONLY,
)
from .subagent_execution import (
    _SUBAGENT_CANCELLATION_TOKEN_ARG as _SUBAGENT_CANCELLATION_TOKEN_ARG,
)
from .subagent_execution import (
    ChildRunRegistry,
    ChildScheduler,
    SubagentLauncher,
)
from .subagent_execution import (
    _create_session_for_subagent as _create_session_for_subagent,
)
from .subagent_execution import (
    _latest_subagent_message_text as _latest_subagent_message_text,
)
from .subagent_execution import (
    _latest_subagent_store_final_text as _latest_subagent_store_final_text,
)
from .subagent_execution import (
    _persist_internal_subagent_report as _persist_internal_subagent_report,
)
from .subagent_execution import (
    _resolve_subagent_final_text as _resolve_subagent_final_text,
)
from .subagent_execution import (
    _subagent_artifact_requirement as _subagent_artifact_requirement,
)
from .subagent_execution import (
    _subagent_exact_tool_catalog_message as _subagent_exact_tool_catalog_message,
)
from .subagent_execution import (
    _subagent_final_report_problem as _subagent_final_report_problem,
)
from .subagent_execution import (
    _subagent_success_event_types as _subagent_success_event_types,
)
from .subagent_execution import (
    _subagent_termination_kind as _subagent_termination_kind,
)
from .subagent_workspace import SubagentWorkspaceProvider
from .verification_commands import (
    _expand_simple_verify_command_chain,
    _has_disallowed_shell_control_flow,
    _verify_run_commands_match_effective_contract,
)
from .verification_evidence import (
    VerificationEvidence,
    VerificationEvidenceCategory,
    _evidence_v2_enabled,
    classify_verification_evidence,
    command_is_qualifying_execution_evidence,
)

_turn_snapshot = importlib.import_module("alysis_code.agent.turn.snapshot")
_SHELL_MUTATION_SNAPSHOT_METADATA_PREFIX = _turn_snapshot._SHELL_MUTATION_SNAPSHOT_METADATA_PREFIX
_detect_command_mutation_paths = _turn_snapshot._detect_command_mutation_paths
_list_git_workspace_snapshot_paths = _turn_snapshot._list_git_workspace_snapshot_paths
_normalize_snapshot_ignore_paths = _turn_snapshot._normalize_snapshot_ignore_paths
_path_matches_snapshot_ignore = _turn_snapshot._path_matches_snapshot_ignore
_run_with_command_mutation_detection = _turn_snapshot._run_with_command_mutation_detection
_snapshot_workspace_for_command_mutation_detection = (
    _turn_snapshot._snapshot_workspace_for_command_mutation_detection
)
_walk_workspace_snapshot_paths = _turn_snapshot._walk_workspace_snapshot_paths
_workspace_snapshot_signature = _turn_snapshot._workspace_snapshot_signature


def _call_with_optional_kwargs(
    func: Callable[..., Any],
    *,
    required_kwargs: dict[str, Any],
    optional_kwargs: dict[str, Any],
) -> Any:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(**required_kwargs, **optional_kwargs)
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    accepted_kwargs = dict(required_kwargs)
    for key, value in optional_kwargs.items():
        if accepts_var_kwargs or key in signature.parameters:
            accepted_kwargs[key] = value
    return func(**accepted_kwargs)


def _command_mutation_metadata(
    *,
    root: Path,
    touched_repo_paths: list[str],
    command_was_verification: bool = False,
) -> dict[str, Any]:
    classifications = classify_mutation_paths(
        touched_repo_paths,
        root=root,
        command_was_verification=command_was_verification,
    )
    material = [item.path for item in classifications if item.is_material]
    benign = [item.path for item in classifications if not item.is_material]
    out: dict[str, Any] = {
        "mutation_path_classifications": [item.as_payload() for item in classifications],
    }
    if material:
        out["material_touched_repo_paths"] = material
    if benign:
        out["benign_runtime_paths"] = benign
    return out


def _verification_relevant_material_paths(paths: list[str]) -> list[str]:
    if not paths or not _paths_require_verification(set(paths)):
        return []
    return list(paths)


def _aggregate_tool_evidence_payload(records: list[VerificationEvidence]) -> dict[str, Any]:
    if not records:
        return {
            "verification_evidence_category": VerificationEvidenceCategory.NOT_VERIFICATION.value,
            "verification_evidence_reason": "no_verification_evidence",
            "verification_evidence_allowed": False,
            "verification_evidence_supplemental_only": False,
        }
    priority = {
        VerificationEvidenceCategory.AUTHORITATIVE: 0,
        VerificationEvidenceCategory.REPO_NATIVE: 1,
        VerificationEvidenceCategory.TASK_ACCEPTANCE: 2,
        VerificationEvidenceCategory.NOT_VERIFICATION: 3,
    }
    primary = sorted(records, key=lambda item: priority[item.category])[0]
    allowed = all(
        item.allowed_to_satisfy_contract
        for item in records
        if item.category != VerificationEvidenceCategory.NOT_VERIFICATION
    )
    if any(item.category == VerificationEvidenceCategory.NOT_VERIFICATION for item in records):
        allowed = False
    return {
        "verification_evidence_category": primary.category.value,
        "verification_evidence_reason": (
            primary.reason
            if allowed
            else next(
                (item.reason for item in records if not item.allowed_to_satisfy_contract),
                primary.reason,
            )
        ),
        "verification_evidence_allowed": allowed,
        "verification_evidence_supplemental_only": all(item.supplemental_only for item in records),
        "verification_evidence_records": [item.as_payload() for item in records],
    }


def _custom_tool_plugin_id(
    tool: CustomToolSpec,
    index: _PluginActivationIndex,
) -> str | None:
    parts = PurePosixPath(tool.relative_tool_path).parts
    if len(parts) >= 2 and parts[0] == "plugins":
        return index.slug_to_plugin_id.get(parts[1])
    return None


def _filter_custom_tool_session_state_for_plugins(
    *,
    state: CustomToolSessionState,
    activation_decision: ActivationDecision,
    index: _PluginActivationIndex,
) -> tuple[CustomToolSessionState, Counter[str]]:
    dropped_counts: Counter[str] = Counter()
    keep_cache: dict[str, bool] = {}

    def keep(tool: CustomToolSpec) -> bool:
        cache_key = os.fspath(tool.source_path)
        if cache_key in keep_cache:
            return keep_cache[cache_key]
        allowed = _component_plugin_allowed(
            _custom_tool_plugin_id(tool, index),
            activation_decision,
            dropped_counts,
        )
        keep_cache[cache_key] = allowed
        return allowed

    filtered_discovery = CustomToolDiscoveryResult(
        global_tools=tuple(tool for tool in state.discovery.global_tools if keep(tool)),
        project_tools=tuple(tool for tool in state.discovery.project_tools if keep(tool)),
        effective_tools=tuple(tool for tool in state.discovery.effective_tools if keep(tool)),
        shadowed_tools=tuple(tool for tool in state.discovery.shadowed_tools if keep(tool)),
        issues=state.discovery.issues,
    )
    return (
        CustomToolSessionState(
            discovery=filtered_discovery,
            trust_state=state.trust_state,
            catalog_entries=tuple(
                entry for entry in state.catalog_entries if entry.spec is None or keep(entry.spec)
            ),
            effective_tools_by_name=filtered_discovery.effective_tools_by_name(),
            exposed_tools_by_name={
                name: tool for name, tool in state.exposed_tools_by_name.items() if keep(tool)
            },
        ),
        dropped_counts,
    )


def _mcp_server_plugin_id(server: ResolvedMcpServer) -> str | None:
    raw = str(server.id or "")
    if "/" not in raw:
        return None
    return normalize_extension_id(raw.split("/", 1)[0])


def _filter_mcp_config_for_plugins(
    *,
    config: ResolvedMcpConfig,
    activation_decision: ActivationDecision,
) -> tuple[ResolvedMcpConfig, Counter[str]]:
    dropped_counts: Counter[str] = Counter()
    servers = tuple(
        server
        for server in config.servers
        if _component_plugin_allowed(
            _mcp_server_plugin_id(server),
            activation_decision,
            dropped_counts,
        )
    )
    return replace(config, servers=servers), dropped_counts


FULLACCESS_DENYLIST_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+/\s*$",
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+/\*",
    r"\bgit\s+push\s+.*--force.*\b(main|master)\b",
    r"\bsudo\b",
    r"\bcurl\s+[^|]*\|\s*sh\b",
    r"\bwget\s+[^|]*\|\s*sh\b",
    r"\bdd\s+if=/dev/",
    r"\bmkfs\.",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",
    r"\bchmod\s+-R\s+777\s+/",
    r">\s*/dev/sd[a-z]",
]


def _normalize_fullaccess_shell_command(cmd: str) -> str:
    return " ".join(str(cmd).strip().split())


def _fullaccess_denylist_match(cmd: str) -> str | None:
    normalized = _normalize_fullaccess_shell_command(cmd)
    for pattern in FULLACCESS_DENYLIST_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return pattern
    return None


def _fullaccess_shell_audit_ts() -> str:
    return datetime.now(UTC).isoformat()


class ToolDispatchGuard(Protocol):
    """Host-supplied veto hook applied before every tool dispatch."""

    def check_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        resolve_rel_path: Callable[..., str] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any]], dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_openai_tool(self) -> dict[str, Any]:
        family = _model_schema_family(self.metadata)
        description_max_chars = self.metadata.get("model_description_max_chars")
        if family in CUSTOM_MCP_SCHEMA_FAMILIES:
            description_max_chars = _schema_description_max_chars(description_max_chars)
        description = str(self.metadata.get("model_description") or self.description)
        description = _model_facing_tool_description(
            description,
            max_chars=description_max_chars,
        )
        parameters = self.parameters
        if bool(self.metadata.get("compact_parameters_for_model")):
            if family in CUSTOM_MCP_SCHEMA_FAMILIES:
                parameters = compact_custom_mcp_tool_parameters(self.parameters)
            else:
                parameters = _drop_model_facing_schema_prose(self.parameters)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": parameters,
            },
        }


_MODEL_FACING_SCHEMA_PROSE_KEYS = frozenset(
    {
        "$comment",
        "description",
        "example",
        "examples",
        "markdownDescription",
        "title",
    }
)


def _model_schema_family(metadata: dict[str, Any]) -> str:
    tool_type = str(metadata.get("tool_type") or "").strip().lower()
    if tool_type == "custom_tool":
        return "custom"
    if tool_type in {"mcp", "mcp_tool"}:
        return "mcp"
    return tool_type


def _schema_description_max_chars(value: Any) -> int:
    try:
        configured = int(value)
    except (TypeError, ValueError):
        configured = 0
    if configured <= 0:
        return DEFAULT_CUSTOM_MCP_DESCRIPTION_MAX_CHARS
    return configured


def _model_facing_tool_description(description: str, *, max_chars: Any) -> str:
    text = " ".join(str(description or "").split())
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _drop_model_facing_schema_prose(value: Any) -> Any:
    if isinstance(value, dict):
        reduced: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in _MODEL_FACING_SCHEMA_PROSE_KEYS:
                continue
            if normalized_key in {"const", "default", "enum"}:
                reduced[key] = copy.deepcopy(item)
                continue
            reduced[key] = _drop_model_facing_schema_prose(item)
        return reduced
    if isinstance(value, list):
        return [_drop_model_facing_schema_prose(item) for item in value]
    return copy.deepcopy(value)


def _drop_schema_descriptions(value: Any) -> Any:
    return _drop_model_facing_schema_prose(value)


_BUILTIN_MODEL_DESCRIPTIONS: dict[str, str] = {
    "report_blocker": "Report a final unresolvable blocker.",
    "fs_read": "Read a workspace text file.",
    "fs_read_lines": "Read numbered file lines.",
    "fs_edit": "Edit one UTF-8 file.",
    "fs_move": "Move a file.",
    "fs_copy": "Copy a file.",
    "fs_delete": "Delete a file.",
    "fs_write": "Write a UTF-8 text file.",
    "fs_mkdir": "Create a directory.",
    "fs_list": "List workspace files.",
    "web_fetch": "Fetch a supplied/search URL.",
    "web_search": (
        "Search current sources for unstable/requested facts; includes UTC retrieved_at. "
        "Fetch URLs with web_fetch."
    ),
    "symbol_search": "Find symbols/snippets.",
    "test_discover": "Suggest tests.",
    "repo_map": "Map code and tests.",
    "search_rg": "Search workspace text.",
    "history_search": "Search prior events.",
    "session_artifact_read": "Read current-session artifact locator.",
    "verify_run": "Run verification.",
    "shell_run": "Run a policy-checked shell command.",
    "shell_background": ("Run a session lifetime command; killed when this session ends."),
    "shell_service_start": (
        "Start a durable service with durable lifetime; keeps running after this session ends."
    ),
    "workspace_preview_start": ("Serve files without Docker; semantic access chooses a free port."),
    "shell_service_status": "Check a service that outlives the session.",
    "shell_service_stop": ("Stop a durable service; others keep running after the session ends."),
    "shell_output": "Read process output.",
    "shell_wait": "Wait for output/exit.",
    "shell_kill": "Stop a background process.",
    "shell_list": "List background processes.",
    "session_set_workdir": "Set active_workdir.",
    "switch_mode": "Propose a user-approved persona switch.",
    "subagent_run": "Run child; eligible batches parallelize max4.",
    "subagent_spawn": (
        "Background: shared read-only, isolated writable. Example: run_id=impl; verifier "
        "depends_on=[impl], workspace_from_run=impl."
    ),
    "subagent_send": "Message a queued/running child.",
    "subagent_resume": "Resume a terminal child as a linked run.",
    "subagent_status": "List children.",
    "subagent_wait": "Collect children.",
    "subagent_cancel": "Cancel children.",
    "subagent_apply": "Apply patch.",
    "subagent_discard": "Discard worktree.",
    "git_status": "Read Git status.",
    "git_diff": "Run git diff.",
    "git_history": "Read Git history.",
    "git_apply_patch": "Apply a unified Git diff.",
    "browser_start": "Start an approval-gated, IDE-owned browser for public websites.",
    "browser_navigate": "Navigate an owned browser to an approved public HTTP(S) URL.",
    "browser_snapshot": "Read a bounded page snapshot from an owned browser.",
    "browser_screenshot": "Capture a screenshot and return only its opaque artifact id.",
    "browser_artifact_read": "Read a bounded base64 chunk of a browser artifact by opaque id.",
    "browser_diagnostics": "Read bounded, redacted browser console and network events.",
    "browser_click": "Click an approved selector in an owned browser.",
    "browser_type": "Type approved text into a selector without echoing the text.",
    "browser_status": "Read one owned browser session status.",
    "browser_list": "List browser sessions owned by this IDE task.",
    "browser_close": "Close an approved owned browser session.",
    "ide_task_list": "List bounded VS Code workspace tasks exposed by the trusted IDE host.",
    "ide_task_run": "Start one opaque VS Code workspace task through the trusted IDE host.",
    "ide_task_status": "Read bounded status for trusted-host VS Code task executions.",
    "ide_task_terminate": "Terminate one VS Code task execution by opaque execution id.",
    "ide_debug_list": "List bounded VS Code debug configurations exposed by the trusted IDE host.",
    "ide_debug_start": "Start one opaque VS Code debug configuration through the trusted IDE host.",
    "ide_debug_stop": "Stop one VS Code debug session by opaque session id.",
    "ide_debug_status": "Read bounded status for trusted-host VS Code debug sessions.",
}


def _tool_event_metadata(tool: ToolDef | None) -> dict[str, Any]:
    if tool is None or not tool.metadata:
        return {}
    metadata = copy.deepcopy(tool.metadata)
    event_metadata: dict[str, Any] = {}
    tool_type = str(metadata.get("tool_type") or "").strip()
    if tool_type:
        event_metadata["tool_type"] = tool_type
    custom_tool = metadata.get("custom_tool")
    if isinstance(custom_tool, dict):
        event_metadata["custom_tool"] = {
            key: value for key, value in custom_tool.items() if key != "output_schema"
        }
    return event_metadata


def _custom_tool_capability_summary(spec: Any) -> str:
    capabilities = getattr(spec, "capabilities", None)
    if capabilities is None:
        return "capabilities: unspecified"
    secret_refs = getattr(capabilities, "secret_refs", ())
    secret_summary = ", ".join(secret_refs) if secret_refs else "-"
    network_hosts = getattr(capabilities, "network_hosts", ())
    network_hosts_summary = ", ".join(network_hosts) if network_hosts else "-"
    return (
        "capabilities: "
        f"read_only={bool(getattr(capabilities, 'read_only', False))}, "
        f"destructive={bool(getattr(capabilities, 'destructive', False))}, "
        f"network={getattr(capabilities, 'network_access', 'unspecified')}, "
        f"network_hosts={network_hosts_summary}, "
        f"fs_read={getattr(capabilities, 'filesystem_read_scope', 'unspecified')}, "
        f"fs_write={getattr(capabilities, 'filesystem_write_scope', 'unspecified')}, "
        f"process_spawn={getattr(capabilities, 'process_spawn', 'unspecified')}, "
        f"secrets={secret_summary}"
    )


_READONLY_MAIN_SESSION_BUILTIN_TOOL_NAMES = frozenset(
    built_in_subagent_tool_names(exposure="readonly")
)


_READONLY_TOP_LEVEL_WEB_TOOL_NAMES = frozenset({"web_fetch", "web_search"})


# Out-of-band channel for handing the turn's cancellation token to the shell
# wait path, mirroring _SUBAGENT_CANCELLATION_TOKEN_ARG. The key is an object()
# rather than a string so it can never collide with a model-supplied argument,
# is skipped by the ``isinstance(key, str)`` filters that build public args, and
# never reaches the schema, the transcript, or the provider.
_SHELL_CANCELLATION_TOKEN_ARG = object()

# Tools whose dispatch can block on a running process, and which therefore need
# the cancellation token so PR2's watchdog can preempt a wait already in flight.
_SHELL_CANCELLABLE_WAIT_TOOL_NAMES = frozenset({"shell_wait"})


def _built_in_tool_exposed_in_mode(
    *,
    tool_name: str,
    mode: str,
    subagent_depth: int = 0,
    readonly_child_web_tool_names: Collection[str] | None = None,
) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode != "readonly":
        return True
    normalized_tool_name = str(tool_name or "").strip()
    if normalized_tool_name in _READONLY_MAIN_SESSION_BUILTIN_TOOL_NAMES:
        return True
    if normalized_tool_name not in _READONLY_TOP_LEVEL_WEB_TOOL_NAMES:
        return False
    # Top-level Plan/readonly sessions can safely use bounded web discovery and
    # fetch tools. At depth one, only a definition-gated research child receives
    # the explicitly allowlisted web tools; helpers at depth two remain narrow.
    if subagent_depth == 0:
        return True
    allowed_child_web_tools = {
        str(name or "").strip() for name in (readonly_child_web_tool_names or ())
    }
    return subagent_depth == 1 and normalized_tool_name in allowed_child_web_tools


def _mcp_tool_exposed_in_mode(*, mode: str, write_scope_restricted: bool = False) -> bool:
    return str(mode or "").strip().lower() != "readonly" and not write_scope_restricted


def _custom_tools_write_scope_restricted(
    *,
    mode: str,
    deny_write_prefixes: list[str] | None,
    allow_write_globs: list[str] | None,
    persona_allow_write_globs: list[str] | None,
) -> bool:
    if persona_allow_write_globs is not None:
        return True
    if str(mode or "").strip().lower() == _MODE_FULLACCESS:
        return False
    if allow_write_globs is not None:
        return True
    always_protected = {
        _normalize_rel_match_path(prefix).casefold()
        for prefix in ALWAYS_PROTECTED_WRITE_PREFIXES
        if _normalize_rel_match_path(prefix)
    }
    for raw in deny_write_prefixes or []:
        cleaned = _normalize_rel_match_path(str(raw))
        if cleaned and cleaned.casefold() not in always_protected:
            return True
    return False


def _unified_diff(old: str, new: str, path: str) -> str:
    import difflib

    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def build_tools(
    *,
    root: Path,
    console: Any | None,
    surface: Surface | None = None,
    store: SessionStore,
    mode: str,
    yes: bool,
    cfg: AppConfig | None = None,
    api_key: str | None = None,
    max_steps: int | None = None,
    no_log: bool = False,
    usage_role: str = "main",
    usage_summary: UsageSummary | None = None,
    model_registry: ModelRegistry | None = None,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    persona_allow_write_globs: list[str] | None = None,
    non_interactive: bool = False,
    shell_runner: Any | None = None,
    process_group_registry: ProcessGroupRegistry | None = None,
    terminal_manager: TerminalManager | None = None,
    durable_service_manager: DurableServiceManager | None = None,
    persistent_service_registry: PersistentServiceRegistry | None = None,
    edit_discipline: EditDisciplineState | None = None,
    verification_enabled: bool = True,
    authoritative_verification_commands: list[str] | None = None,
    effective_verification_commands: list[str] | None = None,
    verify_command_selection: ResolvedVerifyCommands | None = None,
    get_verify_command_selection: Callable[[], ResolvedVerifyCommands | None] | None = None,
    one_shot_execution: bool = False,
    completion_gate_tools_enabled: bool = False,
    skills_enabled: bool = True,
    skill_registry: dict[str, SkillBundle] | None = None,
    subagents_enabled: bool = False,
    helper_subagents_enabled: bool = False,
    subagent_depth: int = 0,
    subagent_registry: dict[str, SubagentDefinition] | None = None,
    session_log_dir_override: Path | None = None,
    step_budget_runtime: Any | None = None,
    emit_web_search_runtime_diagnostics: bool = False,
    runtime_kind: RuntimeKind | str = RuntimeKind.ONE_SHOT,
    persona_switch_state: Any | None = None,
    mcp_manager: McpManager | ForgeTaskScopedMcpManager | None = None,
    custom_tool_session_state: CustomToolSessionState | None = None,
    get_active_workdir_relpath: Callable[[], str] | None = None,
    set_active_workdir_callback: Callable[[str, str], dict[str, Any]] | None = None,
    create_session_factory: Callable[..., Any] | None = None,
    prompt_cache_parent_session_id: str | None = None,
    execution_deadline: ExecutionDeadline | None = None,
    crash_diagnostic_log_path: str | os.PathLike[str] | None = None,
    crash_diagnostics: CrashDiagnosticLogger | None = None,
    tool_dispatch_guard: ToolDispatchGuard | None = None,
    managed_browser_service: ManagedBrowserService | None = None,
    managed_browser_owner_id: str | None = None,
    managed_browser_cancel_check: Callable[[], bool] | None = None,
    host_action_handler: HostActionHandler | None = None,
    host_action_capabilities: Collection[str] | None = None,
    child_scheduler_sink: Callable[[ChildScheduler], None] | None = None,
    parent_steer_inbox: SteerInbox | None = None,
    read_ledger_sink: Callable[[SessionReadLedger], None] | None = None,
    readonly_child_web_tool_names: Collection[str] | None = None,
    child_managed_browser_tool_names: Collection[str] | None = None,
) -> dict[str, ToolDef]:
    root = root.resolve()
    workspace_context = resolve_workspace_context(root)
    surface = surface or NoopSurface()
    host_managed_approvals = bool(
        getattr(surface, "host_managed_approvals", False)
        or getattr(getattr(surface, "_parent_surface", None), "host_managed_approvals", False)
    )
    resolved_runtime_kind = normalize_runtime_kind(
        runtime_kind, fallback=RuntimeKind.INTERACTIVE_CHAT
    )
    authoritative_verify_commands = _normalized_authoritative_verify_commands(
        authoritative_verification_commands
    )
    static_verify_selection = verify_command_selection
    normalized_effective_verification_commands = _normalized_verify_commands(
        effective_verification_commands
        or (
            list(static_verify_selection.commands)
            if isinstance(static_verify_selection, ResolvedVerifyCommands)
            else []
        )
    )
    effective_host_actions = normalized_host_action_capabilities(host_action_capabilities)
    read_ledger = SessionReadLedger(
        root=root,
        enabled=bool(getattr(cfg, "read_ledger_enabled", True)),
    )
    if read_ledger_sink is not None:
        read_ledger_sink(read_ledger)

    def _deadline_payload() -> dict[str, Any]:
        if execution_deadline is None:
            return {
                "failure_category": "deadline",
                "deadline_exhausted": False,
                "remaining_seconds": None,
                "deadline": None,
            }
        remaining = execution_deadline.remaining_seconds()
        return {
            "failure_category": "deadline",
            "deadline_exhausted": execution_deadline.is_exhausted(),
            "remaining_seconds": remaining,
            "deadline": execution_deadline.telemetry_snapshot(),
        }

    def _deadline_error(
        message: str,
        *,
        prevented_launch: bool = True,
        start_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "error": message,
            "deadline_prevented_launch": prevented_launch,
            **_deadline_payload(),
        }
        if start_decision is not None:
            payload["deadline_start_decision"] = start_decision
        if crash_diagnostics is not None:
            crash_diagnostics.event(
                "deadline_exhausted",
                {
                    "operation": "tool",
                    "deadline_exhausted": payload["deadline_exhausted"],
                    "remaining_seconds": payload["remaining_seconds"],
                    "deadline": payload["deadline"],
                    "deadline_start_decision": start_decision,
                },
                durable=True,
            )
        return payload

    def _deadline_warning_fields(
        message: str,
        *,
        start_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "deadline_warning": message,
            "deadline_prevented_launch": False,
            **_deadline_payload(),
        }
        if start_decision is not None:
            payload["deadline_start_decision"] = start_decision
        if crash_diagnostics is not None:
            crash_diagnostics.event(
                "deadline_exhausted",
                {
                    "operation": "tool",
                    "deadline_exhausted": payload["deadline_exhausted"],
                    "remaining_seconds": payload["remaining_seconds"],
                    "deadline": payload["deadline"],
                    "deadline_start_decision": start_decision,
                },
                durable=True,
            )
        return payload

    def _deadline_start_decision(
        operation: DeadlineOperation,
        *,
        minimum_remaining_seconds: float,
        configured_timeout_seconds: float | None = None,
        allow_during_finalization: bool = False,
    ) -> dict[str, Any] | None:
        if execution_deadline is None:
            return None
        return execution_deadline.start_decision(
            operation,
            minimum_remaining_seconds=minimum_remaining_seconds,
            configured_timeout_seconds=configured_timeout_seconds,
            allow_during_finalization=allow_during_finalization,
        ).telemetry_snapshot()

    def _deadline_timeout(
        configured_timeout_seconds: float,
        *,
        operation: str,
    ) -> float:
        timeout = deadline_timeout_or_raise(
            execution_deadline,
            configured_timeout_seconds,
            reserve_seconds=DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS,
            operation=operation,
        )
        return float(configured_timeout_seconds if timeout is None else timeout)

    def _current_verify_selection() -> ResolvedVerifyCommands | None:
        if callable(get_verify_command_selection):
            try:
                current = get_verify_command_selection()
            except Exception:  # noqa: BLE001
                current = None
            if isinstance(current, ResolvedVerifyCommands):
                return current
        if isinstance(static_verify_selection, ResolvedVerifyCommands):
            return static_verify_selection
        if authoritative_verify_commands is not None:
            return ResolvedVerifyCommands(
                commands=tuple(authoritative_verify_commands),
                source="environment.authoritative_verification_commands",
                reason="managed runtime injected authoritative verification commands",
                contract_type="authoritative_override",
            )
        if normalized_effective_verification_commands:
            return ResolvedVerifyCommands(
                commands=tuple(normalized_effective_verification_commands),
                source="session.effective_verification_commands",
                reason="session already resolved an effective verification contract",
                contract_type="selected",
            )
        return None

    command_mutation_tracking_enabled = bool(
        resolved_runtime_kind == RuntimeKind.SUBAGENT
        or (
            subagent_depth == 0
            and (one_shot_execution or resolved_runtime_kind == RuntimeKind.INTERACTIVE_CHAT)
        )
    )
    command_mutation_ignored_paths: list[Path] = []
    if command_mutation_tracking_enabled:
        command_mutation_ignored_paths = [
            candidate
            for candidate in [
                getattr(store, "path", None),
                getattr(store, "session_artifact_root", None),
            ]
            if isinstance(candidate, Path)
        ]
    history_artifact_persistence_available = bool(
        getattr(store, "enabled", False) or session_log_dir_override is not None
    )
    git_backed_workspace = workspace_context.git_root is not None
    resolved_skill_registry = dict(skill_registry or {})
    built_in_tool_names = {spec.name.casefold() for spec in iter_builtin_tool_metadata()}
    custom_tool_session_state = build_custom_tool_session_state(
        workspace_root=root,
        custom_tools_enabled=bool(getattr(cfg, "custom_tools_enabled", True)) if cfg else True,
        mode=mode,
        runtime_kind=resolved_runtime_kind,
        built_in_tool_names=built_in_tool_names,
        write_scope_restricted=_custom_tools_write_scope_restricted(
            mode=mode,
            deny_write_prefixes=deny_write_prefixes,
            allow_write_globs=allow_write_globs,
            persona_allow_write_globs=persona_allow_write_globs,
        ),
        discovery=(
            custom_tool_session_state.discovery if custom_tool_session_state is not None else None
        ),
        trust_state=(
            custom_tool_session_state.trust_state if custom_tool_session_state is not None else None
        ),
    )

    persona_write_scope_active = persona_allow_write_globs is not None
    # A persona scope remains a real host constraint even if a caller ever
    # constructs an inconsistent fullaccess+scope session. Normal persona
    # application also clamps that combination to review.
    is_full_access_mode = mode == _MODE_FULLACCESS and not persona_write_scope_active
    deny_prefixes: list[str] = []
    if not is_full_access_mode:
        seen_deny_prefixes: set[str] = set()
        for raw in [
            *ALWAYS_PROTECTED_WRITE_PREFIXES,
            *(deny_write_prefixes or []),
        ]:
            cleaned = _normalize_rel_match_path(str(raw))
            if cleaned:
                normalized = cleaned.casefold()
                if normalized not in seen_deny_prefixes:
                    seen_deny_prefixes.add(normalized)
                    deny_prefixes.append(cleaned)
    deny_prefixes_cf = [pref.casefold() for pref in deny_prefixes]
    allow_pattern_groups: list[list[str]] = []
    allowed_ancestor_dir_groups_cf: list[set[str]] = []
    if not is_full_access_mode:
        for raw_group in (allow_write_globs, persona_allow_write_globs):
            if raw_group is None:
                continue
            patterns = [
                cleaned for raw in raw_group if (cleaned := _normalize_rel_match_path(str(raw)))
            ]
            allow_pattern_groups.append(patterns)
            allowed_ancestor_dir_groups_cf.append(
                {
                    cleaned.casefold()
                    for path in ancestor_directory_scope_patterns(raw_group)
                    if (cleaned := _normalize_rel_match_path(path))
                }
            )

    def _is_denied_path(rel_path: str) -> bool:
        if not deny_prefixes_cf:
            return False
        rel_norm = _normalize_rel_match_path(rel_path)
        rel_cf = rel_norm.casefold()
        for pref_cf in deny_prefixes_cf:
            if rel_cf == pref_cf or rel_cf.startswith(pref_cf + "/"):
                return True
        return False

    def _path_escape_recovery_payload(
        *,
        tool_name: str,
        attempted_path: str,
        field_name: str,
        workspace_root: Path,
        path_base: str | None = None,
    ) -> dict[str, Any]:
        base_note = f" with path_base={path_base}" if path_base else ""
        normalized_tool = str(tool_name or "").strip().lower()
        write_tools = {
            "fs_write",
            "fs_edit",
            "fs_move",
            "fs_copy",
            "fs_delete",
            "fs_mkdir",
        }
        shell_cwd_tools = {"shell_run", "shell_background", "shell_service_start"}

        if normalized_tool in write_tools:
            guidance = (
                "Use a workspace-relative path for this filesystem write. If the user "
                "explicitly requested an absolute path outside the workspace, explain that "
                "filesystem write tools cannot do that. Use shell_run only when policy and "
                "any required user approval allow the explicit external write."
            )
            suggested_next_actions = [
                {
                    "action": "use_workspace_relative_path",
                    "description": "Retry with a path inside the workspace.",
                    "requires_user_confirmation": False,
                },
                {
                    "action": "use_shell_run_if_policy_allows",
                    "description": (
                        "Use a specific shell command for an explicitly requested external "
                        "target only when policy and approvals allow it."
                    ),
                    "requires_user_confirmation": True,
                },
                {
                    "action": "ask_or_explain_boundary",
                    "description": "Explain the workspace boundary and ask how to proceed.",
                    "requires_user_confirmation": False,
                },
            ]
            can_use_other_allowed_tool = (
                "shell_run may target an absolute path only when policy and any required "
                "approval allow it"
            )
            requires_user_confirmation = True
        elif normalized_tool in shell_cwd_tools or field_name == "cwd":
            guidance = (
                "Use a cwd inside the workspace. If the command needs an external path, keep "
                "cwd workspace-relative and pass the path explicitly only when shell policy "
                "and approvals allow that operation."
            )
            suggested_next_actions = [
                {
                    "action": "use_workspace_relative_cwd",
                    "description": "Retry with cwd omitted or set inside the workspace.",
                    "requires_user_confirmation": False,
                },
                {
                    "action": "pass_external_path_as_argument_if_policy_allows",
                    "description": (
                        "Keep cwd inside the workspace and pass the external path explicitly "
                        "only when policy and approvals allow it."
                    ),
                    "requires_user_confirmation": True,
                },
                {
                    "action": "ask_or_explain_boundary",
                    "description": "Explain the cwd boundary and ask how to proceed.",
                    "requires_user_confirmation": False,
                },
            ]
            can_use_other_allowed_tool = "Shell commands must start from a workspace-relative cwd"
            requires_user_confirmation = True
        else:
            guidance = (
                "Use a workspace-relative path. This tool cannot inspect arbitrary paths "
                "outside the workspace; ask the user to move the input into the workspace or "
                "provide its contents."
            )
            suggested_next_actions = [
                {
                    "action": "use_workspace_relative_path",
                    "description": "Retry with a path inside the workspace.",
                    "requires_user_confirmation": False,
                },
                {
                    "action": "ask_user_for_accessible_input",
                    "description": "Ask the user to provide the input inside the workspace.",
                    "requires_user_confirmation": False,
                },
                {
                    "action": "explain_boundary",
                    "description": "Explain that the tool cannot access the external path.",
                    "requires_user_confirmation": False,
                },
            ]
            can_use_other_allowed_tool = (
                "No filesystem read or search tool can access paths outside the workspace"
            )
            requires_user_confirmation = False

        return {
            "error": (
                f"Path escapes root ({field_name}): {attempted_path}. Workspace path arguments "
                f"are limited to {os.fspath(workspace_root)}{base_note}. Recovery: {guidance}"
            ),
            "error_code": "path_escapes_workspace",
            "code": "path_escapes_workspace",
            "attempted_path": attempted_path,
            "path_field": field_name,
            "tool_name": normalized_tool or tool_name,
            "workspace_root": os.fspath(workspace_root),
            "rule": "workspace path arguments must resolve under workspace_root",
            "can_use_other_allowed_tool": can_use_other_allowed_tool,
            "requires_user_confirmation": requires_user_confirmation,
            "guidance": guidance,
            "suggested_next_actions": suggested_next_actions,
        }

    def _resolve_rel_path(rel_path: str) -> str:
        root_abs = root.resolve()
        target = (root_abs / rel_path).resolve()
        try:
            normalized = target.relative_to(root_abs)
        except ValueError as e:
            payload = _path_escape_recovery_payload(
                tool_name="filesystem",
                attempted_path=rel_path,
                field_name="path",
                workspace_root=root_abs,
            )
            raise AgentRuntimeError(str(payload["error"]), result_payload=payload) from e
        return os.fspath(normalized)

    def _resolve_rel_write_path(rel_path: str) -> str:
        return _resolve_rel_path(rel_path)

    def _guard_write_path(rel_path: str) -> None:
        if is_full_access_mode:
            return
        if _is_denied_path(rel_path):
            raise AgentRuntimeError(f"Blocked write to protected path: {rel_path}")
        rel_norm = _normalize_rel_match_path(rel_path)
        rel_cf = rel_norm.casefold()
        for patterns in allow_pattern_groups:
            in_scope = any(
                scope_path_matches_pattern(rel_norm, pattern, root=root) for pattern in patterns
            )
            if not in_scope:
                in_scope = any(
                    rel_cf == _normalize_rel_match_path(pattern).casefold()
                    for pattern in patterns
                    if not any(ch in pattern for ch in ["*", "?", "["])
                )
            if not in_scope:
                raise AgentRuntimeError(f"Blocked write outside allowed scope: {rel_path}")

    def _is_allowed_ancestor_dir_creation(rel_path: str) -> bool:
        if is_full_access_mode or not allow_pattern_groups:
            return False
        rel_norm = _normalize_rel_match_path(rel_path).casefold()
        return all(rel_norm in ancestors for ancestors in allowed_ancestor_dir_groups_cf)

    def _sensitive_path_findings(paths: list[str]) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for path in paths:
            classification = classify_sensitive_path(path)
            if classification.sensitive:
                findings.append(
                    {
                        "path": path,
                        "category": str(classification.category or "sensitive_file"),
                    }
                )
        return findings

    def guard_sensitive_files(kind: str, *, files: list[str]) -> list[dict[str, str]]:
        """Require one-time human consent that broad/session policy cannot satisfy."""

        findings = _sensitive_path_findings(files)
        if not findings:
            return []
        if non_interactive and not host_managed_approvals:
            raise AgentRuntimeError(
                f"Explicit one-time user approval is required for {kind} on a sensitive file."
            )
        categories = sorted({finding["category"] for finding in findings})
        preview = "\n".join(
            [
                f"Sensitive file operation: {kind}",
                *(f"path: {finding['path']} ({finding['category']})" for finding in findings),
                "File contents are intentionally omitted from this approval preview.",
            ]
        )
        decision = surface.request_approval(
            ApprovalRequest(
                kind=kind,
                reason="sensitive files require an explicit one-time approval",
                preview=preview,
                files=[finding["path"] for finding in findings],
                metadata={
                    "mandatory_explicit_approval": True,
                    "allow_for_session_disabled": True,
                    "sensitive_categories": categories,
                },
                # Deliberately no allow_for_session_scope: stored grants must
                # never authorize current or future sensitive-file access.
                allow_for_session_scope=None,
            )
        )
        if not decision.allow:
            raise ApprovalDeclinedError(kind)
        if decision.allow_for_session:
            # Auto/YOLO surfaces and cached grants identify themselves through
            # allow_for_session. Sensitive access only accepts the UI's one-time
            # allow decision.
            raise AgentRuntimeError(
                f"Automatic or session approval cannot authorize {kind} on a sensitive file. "
                "Switch approvals to ask and approve this operation once."
            )
        return findings

    def guard_sensitive_read(kind: str, *, path: str) -> list[dict[str, str]]:
        findings = _sensitive_path_findings([path])
        if findings and not (root / path).exists():
            message = (
                f"Path does not exist: {path}. This result is terminal; "
                "do not retry this path."
            )
            raise AgentRuntimeError(
                message,
                result_payload={
                    "error": message,
                    "error_code": "fs_path_not_found",
                    "terminal": True,
                    "retryable": False,
                },
            )
        return guard_sensitive_files(kind, files=[path])

    def _mark_sensitive_result(
        result: dict[str, Any], findings: list[dict[str, str]]
    ) -> dict[str, Any]:
        if findings:
            result["_alysis_output_policy"] = {
                "sensitive": True,
                "persist": "redact",
                "display": "redact",
                "categories": sorted({finding["category"] for finding in findings}),
            }
        return result

    def _stale_file_result(error: StaleFileError) -> dict[str, Any]:
        return {
            "error": "The file changed after this operation was prepared; no mutation was made.",
            "error_code": "stale_file",
            "code": "stale_file",
            "path": error.path,
            "recoverable": True,
        }

    def guard_write(kind: str, preview: str, *, files: list[str] | None = None) -> None:
        if is_full_access_mode:
            return
        if mode == "readonly":
            raise AgentRuntimeError(f"Blocked in readonly mode: {kind}")
        if mode == "review":
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "Confirmation required for sensitive command. Re-run with --yes or adjust plan."
                )
            decision = surface.request_approval(
                ApprovalRequest(
                    kind=kind,
                    reason="review mode requires confirmation for write operations",
                    preview=preview,
                    files=files or [],
                    allow_for_session_scope=exact_file_set_scope(files or [], operation=kind)
                    if files
                    else None,
                )
            )
            if not decision.allow:
                raise ApprovalDeclinedError(kind)
        if mode == "auto" and kind == "fs_delete" and not yes:
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "Confirmation required for sensitive command. Re-run with --yes or adjust plan."
                )
            decision = surface.request_approval(
                ApprovalRequest(
                    kind=kind,
                    reason="file deletion requires confirmation",
                    preview=preview,
                    files=files or [],
                    allow_for_session_scope=exact_file_set_scope(files or [], operation=kind)
                    if files
                    else None,
                )
            )
            if not decision.allow:
                raise ApprovalDeclinedError(kind)

    def guard_shell(cmd: str, *, tool_name: str = "shell_run") -> None:
        if persona_write_scope_active:
            raise AgentRuntimeError(
                f"Blocked while persona write scope is active: {tool_name}. "
                "Use scoped filesystem and inspection tools instead."
            )
        if matched_pattern := _fullaccess_denylist_match(cmd):
            if tool_name != "shell_run" and not is_full_access_mode:
                raise AgentRuntimeError(f"Blocked command: denylist pattern {matched_pattern}")
            raise AgentRuntimeError(
                f"Blocked fullaccess shell command by denylist pattern: {matched_pattern}"
            )
        if is_full_access_mode:
            return
        if mode == "readonly":
            raise AgentRuntimeError(f"Blocked in readonly mode: {tool_name}")
        decision = evaluate_shell_command(cmd)
        if not decision.allowed:
            raise AgentRuntimeError(f"Blocked command: {decision.reason}")
        if mode == "review":
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "Confirmation required for sensitive command. Re-run with --yes or adjust plan."
                )
            decision = surface.request_approval(
                ApprovalRequest(
                    kind=tool_name,
                    reason="review mode requires confirmation for shell commands",
                    preview=cmd,
                    command=cmd,
                    allow_for_session_scope=exact_command_scope(cmd, kind=tool_name),
                )
            )
            if not decision.allow:
                raise ApprovalDeclinedError(tool_name)
            return
        # auto mode
        if decision.needs_confirm and not yes:
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "Confirmation required for sensitive command. Re-run with --yes or adjust plan."
                )
            choice = surface.request_approval(
                ApprovalRequest(
                    kind=tool_name,
                    reason=f"sensitive command: {decision.reason}",
                    preview=cmd,
                    command=cmd,
                    allow_for_session_scope=exact_command_scope(cmd, kind=tool_name),
                )
            )
            if not choice.allow:
                raise ApprovalDeclinedError(tool_name)

    def guard_terminal_op(op_name: str) -> None:
        if is_full_access_mode:
            return
        if mode == "readonly":
            raise AgentRuntimeError(f"Blocked in readonly mode: {op_name}")

    def guard_verify(commands: list[str]) -> None:
        if persona_write_scope_active:
            raise AgentRuntimeError(
                "Blocked while persona write scope is active: verify_run. "
                "Verification commands can write paths the persona scope cannot constrain."
            )
        if is_full_access_mode:
            return
        if mode == "readonly":
            raise AgentRuntimeError("Blocked in readonly mode: verify_run")

        sensitive_reason: str | None = None
        for command in commands:
            decision = evaluate_shell_command(command)
            if not decision.allowed:
                raise AgentRuntimeError(f"Blocked command: {decision.reason}")
            if sensitive_reason is None and decision.needs_confirm:
                sensitive_reason = decision.reason

        preview = "\n".join(f"$ {command}" for command in commands)
        command_label = (
            commands[0] if len(commands) == 1 else f"{len(commands)} verification commands"
        )

        if mode == "review":
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "Confirmation required for sensitive command. Re-run with --yes or adjust plan."
                )
            decision = surface.request_approval(
                ApprovalRequest(
                    kind="verify_run",
                    reason="review mode requires confirmation for verification commands",
                    preview=preview,
                    command=command_label,
                    allow_for_session_scope=exact_verify_command_set_scope(commands),
                )
            )
            if not decision.allow:
                raise ApprovalDeclinedError("verify_run")
            return

        if sensitive_reason and not yes:
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "Confirmation required for sensitive command. Re-run with --yes or adjust plan."
                )
            choice = surface.request_approval(
                ApprovalRequest(
                    kind="verify_run",
                    reason=f"sensitive command in verification set: {sensitive_reason}",
                    preview=preview,
                    command=command_label,
                    allow_for_session_scope=exact_verify_command_set_scope(commands),
                )
            )
            if not choice.allow:
                raise ApprovalDeclinedError("verify_run")

    tools: list[ToolDef] = []

    def _default_active_workdir_relpath() -> str:
        return (
            _normalize_workspace_relpath(get_active_workdir_relpath())
            if callable(get_active_workdir_relpath)
            else "."
        )

    def _normalize_tool_path_base(
        raw_value: Any,
        *,
        field_name: str,
        default: str = "active_workdir",
    ) -> str:
        if raw_value is None:
            return default
        text = str(raw_value).strip().lower()
        if not text:
            return default
        if text in {"active_workdir", "workspace_root"}:
            return text
        raise AgentRuntimeError(
            f"Invalid {field_name}: {raw_value!r}. Expected 'active_workdir' or 'workspace_root'."
        )

    def _resolve_workspace_relative_path(
        *,
        tool_name: str,
        raw_path: Any,
        raw_base: Any = None,
        field_name: str,
        base_field_name: str,
        allow_empty: bool = False,
    ) -> str:
        workspace_root = root.resolve()
        base_kind = _normalize_tool_path_base(raw_base, field_name=base_field_name)
        if base_kind == "workspace_root":
            base_path = workspace_root
        else:
            base_path = resolve_workdir_relpath_within_workspace(
                workspace_root=workspace_root,
                relpath=_default_active_workdir_relpath(),
            )

        text = "" if raw_path is None else str(raw_path).strip()
        if not text:
            if allow_empty:
                return _workspace_relpath_for_path(workspace_root=workspace_root, path=base_path)
            raise AgentRuntimeError(f"Missing required argument: {field_name}")

        requested = Path(text)
        candidate = (
            requested.resolve() if requested.is_absolute() else (base_path / requested).resolve()
        )
        try:
            candidate.relative_to(workspace_root)
        except ValueError as e:
            payload = _path_escape_recovery_payload(
                tool_name=tool_name,
                attempted_path=text,
                field_name=field_name,
                workspace_root=workspace_root,
                path_base=base_kind,
            )
            raise AgentRuntimeError(str(payload["error"]), result_payload=payload) from e
        rel_path = _workspace_relpath_for_path(workspace_root=workspace_root, path=candidate)
        if rel_path == "README" and not (workspace_root / "README").exists():
            if (workspace_root / "README.md").exists():
                return "README.md"
        if rel_path == "README.md" and not (workspace_root / "README.md").exists():
            if (workspace_root / "README").exists():
                return "README"
        return rel_path

    def _make_tool_def(
        name: str,
        *,
        run: Callable[[dict[str, Any]], dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> ToolDef:
        metadata = require_builtin_tool_metadata(name)
        return ToolDef(
            name=metadata.name,
            description=metadata.description,
            parameters=parameters if parameters is not None else copied_tool_parameters(name),
            run=run,
            metadata={
                "tool_type": "builtin",
                "compact_parameters_for_model": True,
                "model_description": _BUILTIN_MODEL_DESCRIPTIONS.get(
                    metadata.name, metadata.description
                ),
            },
        )

    def _custom_tool_requires_approval(spec: Any) -> bool:
        if mode == "review":
            return True
        return False

    def _run_custom_tool(spec: Any, args: dict[str, Any]) -> dict[str, Any]:
        if mode == "readonly":
            raise AgentRuntimeError(f"Blocked in readonly mode: custom tool '{spec.name}'")
        args_preview = json.dumps(args, ensure_ascii=True, indent=2, sort_keys=True)
        preview = (
            f"Run custom tool\n"
            f"name: {spec.name}\n"
            f"scope: {spec.source_scope}\n"
            f"path: {spec.source_path}\n"
            f"{_custom_tool_capability_summary(spec)}\n"
            f"args:\n{args_preview}"
        )
        if _custom_tool_requires_approval(spec):
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "Confirmation required for custom tool execution. Re-run with --yes or adjust plan."
                )
            decision = surface.request_approval(
                ApprovalRequest(
                    kind=f"custom_tool_run:{spec.name}",
                    reason="review mode requires confirmation for custom tools",
                    preview=preview,
                    files=[spec.relative_tool_path],
                    command=spec.name,
                    metadata={"custom_tool": spec.metadata(include_output_schema=True)},
                )
            )
            if not decision.allow:
                raise ApprovalDeclinedError(
                    f"custom tool '{spec.name}'",
                    message=f"User declined: custom tool '{spec.name}'",
                )
        artifact_dir: Path | None = None
        artifact_reference_prefix: str | None = None
        if store.artifact_persistence_enabled:
            artifact_dir = store.runtime_artifact_path("tool_logs")
            artifact_reference_prefix = store.session_artifact_layout.artifact_locator("tool_logs")
        return run_custom_tool(
            spec=spec,
            args=args,
            workspace_root=root,
            session_id=store.session_id,
            artifact_dir=artifact_dir,
            artifact_reference_prefix=artifact_reference_prefix,
        )

    def _append_builtin_tool(
        name: str,
        *,
        run: Callable[[dict[str, Any]], dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> None:
        if not _built_in_tool_exposed_in_mode(
            tool_name=name,
            mode=mode,
            subagent_depth=subagent_depth,
            readonly_child_web_tool_names=readonly_child_web_tool_names,
        ):
            return
        tools.append(_make_tool_def(name, run=run, parameters=parameters))

    def _fs_read(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_workspace_relative_path(
            tool_name="fs_read",
            raw_path=args.get("path"),
            raw_base=args.get("path_base"),
            field_name="path",
            base_field_name="path_base",
        )
        sensitive = guard_sensitive_read("fs_read", path=path)
        content_hash_before = read_ledger.content_hash(path)
        result = _patchable("fs_read", fs_read)(
            root=root,
            path=path,
            max_bytes=int(args.get("max_bytes") or 20000),
            allow_derived=bool(args.get("allow_derived", False)),
        )
        result = read_ledger.filter_result(
            path=path,
            result=result,
            content_hash_before=content_hash_before,
            force=args.get("force") is True,
        )
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("fs_read", run=_fs_read)

    # report_blocker is a top-level completion-gate control signal, not a
    # repository action. Keep the runtime-kind check here as a fail-closed
    # boundary even though the session also computes gate eligibility.
    if (
        completion_gate_tools_enabled
        and subagent_depth == 0
        and resolved_runtime_kind
        in {
            RuntimeKind.INTERACTIVE_CHAT,
            RuntimeKind.ONE_SHOT,
            RuntimeKind.FORGE_EXEC,
        }
    ):

        def _report_blocker(args: dict[str, Any]) -> dict[str, Any]:
            raw_message = args.get("message")
            if not isinstance(raw_message, str) or not raw_message.strip():
                return {
                    "error": "message must be a non-empty string",
                    "error_code": "invalid_blocker_message",
                    "reported": False,
                }
            message = raw_message.strip()
            if len(message) > REPORT_BLOCKER_MAX_MESSAGE_CHARS:
                return {
                    "error": (
                        "message exceeds the transport limit of "
                        f"{REPORT_BLOCKER_MAX_MESSAGE_CHARS} characters"
                    ),
                    "error_code": "blocker_message_too_long",
                    "reported": False,
                }
            return {"ok": True, "reported": True, "message": message}

        _append_builtin_tool("report_blocker", run=_report_blocker)

    # switch_mode: model-proposed persona switch, user-approved, applied by the
    # chat loop at turn end (the tool surface is never swapped mid-turn). Only
    # the top-level interactive chat runtime provides persona_switch_state, so
    # one_shot/forge/swarm/subagent/conflict runtimes never see this tool and
    # automation can never switch personas silently.
    if (
        persona_switch_state is not None
        and resolved_runtime_kind == RuntimeKind.INTERACTIVE_CHAT
        and not non_interactive
        and subagent_depth == 0
        and persona_modes_enabled(cfg)
    ):

        def _switch_mode(args: dict[str, Any]) -> dict[str, Any]:
            persona_raw = str(args.get("persona") or "").strip().lower()
            reason = " ".join(str(args.get("reason") or "").split())[:300]
            if not is_persona_name(persona_raw):
                return {
                    "ok": False,
                    "applied": False,
                    "error": "unknown persona; valid: code, architect, ask, debug",
                }
            if persona_switch_state.last_declined == persona_raw:
                return {
                    "ok": True,
                    "applied": False,
                    "declined": True,
                    "note": (
                        "The user already declined switching to this persona in "
                        "this session; continue in the current persona without "
                        "asking again."
                    ),
                }
            decision = surface.request_approval(
                ApprovalRequest(
                    kind="persona_switch",
                    reason=f"model proposes a persona switch: {reason or 'no reason given'}",
                    preview=f"Switch persona to {persona_raw} for the rest of the session?",
                    metadata={"persona": persona_raw},
                )
            )
            if not decision.allow:
                persona_switch_state.last_declined = persona_raw
                return {
                    "ok": True,
                    "applied": False,
                    "declined": True,
                    "note": "User declined; continue in the current persona.",
                }
            persona_switch_state.last_declined = None
            persona_switch_state.pending = (persona_raw, reason)
            return {
                "ok": True,
                "applied": False,
                "scheduled": True,
                "persona": persona_raw,
                "note": "Approved. The persona switch applies when this turn ends.",
            }

        _append_builtin_tool("switch_mode", run=_switch_mode)

    def _fs_read_lines(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_workspace_relative_path(
            tool_name="fs_read_lines",
            raw_path=args.get("path"),
            raw_base=args.get("path_base"),
            field_name="path",
            base_field_name="path_base",
        )
        sensitive = guard_sensitive_read("fs_read_lines", path=path)
        content_hash_before = read_ledger.content_hash(path)
        result = _patchable("fs_read_lines", fs_read_lines)(
            root=root,
            path=path,
            start_line=int(args["start_line"]) if args.get("start_line") is not None else 0,
            end_line=(int(args["end_line"]) if args.get("end_line") is not None else None),
            max_lines=(int(args["max_lines"]) if args.get("max_lines") is not None else 200),
            include_line_numbers=bool(args.get("include_line_numbers", True)),
            max_bytes=(int(args["max_bytes"]) if args.get("max_bytes") is not None else 48_000),
        )
        result = read_ledger.filter_result(
            path=path,
            result=result,
            content_hash_before=content_hash_before,
            force=args.get("force") is True,
        )
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("fs_read_lines", run=_fs_read_lines)

    def _fs_edit(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_workspace_relative_path(
            tool_name="fs_edit",
            raw_path=args.get("path"),
            raw_base=args.get("path_base"),
            field_name="path",
            base_field_name="path_base",
        )
        _guard_write_path(path)
        raw_edits = args.get("edits")
        if not isinstance(raw_edits, list):
            raise FsError("edits must be a non-empty array of edit objects")
        sensitive_findings = _sensitive_path_findings([path])
        stamped_precondition = (
            capture_file_precondition(root=root, path=path) if sensitive_findings else None
        )
        sensitive = guard_sensitive_files("fs_edit", files=[path])
        if stamped_precondition is not None:
            try:
                assert_file_precondition(root=root, precondition=stamped_precondition)
            except StaleFileError as error:
                return _stale_file_result(error)
        try:
            prepared = prepare_fs_edit(root=root, path=path, edits=raw_edits)
        except FsError:
            if sensitive_findings:
                try:
                    assert_file_precondition(root=root, precondition=stamped_precondition)
                except StaleFileError as error:
                    return _stale_file_result(error)
                raise AgentRuntimeError(
                    "Sensitive file edit could not be prepared; content details were redacted."
                ) from None
            raise
        if stamped_precondition is not None:
            if prepared.precondition != stamped_precondition:
                return _stale_file_result(StaleFileError(path))
            prepared = replace(prepared, precondition=stamped_precondition)
        if sensitive:
            store.append(
                "sensitive_change_preview",
                {"path": path, "operation": "fs_edit", "content_redacted": True},
            )
        else:
            diff = _unified_diff(prepared.original_content, prepared.updated_content, path)
            store.append("diff_preview", {"path": path, "diff": diff[:20000]})
            surface.on_patch_generated(
                PatchEvent(
                    files=[path],
                    diff=diff,
                    summary=f"1 file changed via fs_edit ({path})",
                )
            )
            guard_write("fs_edit", diff[:20000] or f"(no diff) {path}", files=[path])
        try:
            result = write_prepared_fs_edit(prepared, root=root)
        except StaleFileError as error:
            return _stale_file_result(error)
        read_ledger.invalidate(path)
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("fs_edit", run=_fs_edit)

    def _fs_move(args: dict[str, Any]) -> dict[str, Any]:
        source_path = _resolve_workspace_relative_path(
            tool_name="fs_move",
            raw_path=args.get("source_path"),
            raw_base=args.get("source_path_base"),
            field_name="source_path",
            base_field_name="source_path_base",
        )
        destination_path = _resolve_workspace_relative_path(
            tool_name="fs_move",
            raw_path=args.get("destination_path"),
            raw_base=args.get("destination_path_base"),
            field_name="destination_path",
            base_field_name="destination_path_base",
        )
        _guard_write_path(source_path)
        _guard_write_path(destination_path)
        overwrite = bool(args.get("overwrite", False))
        source_precondition = capture_file_precondition(root=root, path=source_path)
        destination_precondition = capture_file_precondition(root=root, path=destination_path)
        sensitive = guard_sensitive_files("fs_move", files=[source_path, destination_path])
        preview = (
            "Move file\n"
            f"source: {source_path}\n"
            f"destination: {destination_path}\n"
            f"overwrite: {str(overwrite).lower()}"
        )
        if not sensitive:
            guard_write("fs_move", preview, files=[source_path, destination_path])
        try:
            result = fs_move(
                root=root,
                source_path=source_path,
                destination_path=destination_path,
                overwrite=overwrite,
                source_precondition=source_precondition,
                destination_precondition=destination_precondition,
            )
        except StaleFileError as error:
            return _stale_file_result(error)
        read_ledger.invalidate(source_path, destination_path)
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("fs_move", run=_fs_move)

    def _fs_copy(args: dict[str, Any]) -> dict[str, Any]:
        source_path = _resolve_workspace_relative_path(
            tool_name="fs_copy",
            raw_path=args.get("source_path"),
            raw_base=args.get("source_path_base"),
            field_name="source_path",
            base_field_name="source_path_base",
        )
        destination_path = _resolve_workspace_relative_path(
            tool_name="fs_copy",
            raw_path=args.get("destination_path"),
            raw_base=args.get("destination_path_base"),
            field_name="destination_path",
            base_field_name="destination_path_base",
        )
        _guard_write_path(destination_path)
        overwrite = bool(args.get("overwrite", False))
        source_precondition = capture_file_precondition(root=root, path=source_path)
        destination_precondition = capture_file_precondition(root=root, path=destination_path)
        sensitive = guard_sensitive_files("fs_copy", files=[source_path, destination_path])
        preview = (
            "Copy file\n"
            f"source: {source_path}\n"
            f"destination: {destination_path}\n"
            f"overwrite: {str(overwrite).lower()}"
        )
        if not sensitive:
            guard_write("fs_copy", preview, files=[source_path, destination_path])
        try:
            result = fs_copy(
                root=root,
                source_path=source_path,
                destination_path=destination_path,
                overwrite=overwrite,
                source_precondition=source_precondition,
                destination_precondition=destination_precondition,
            )
        except StaleFileError as error:
            return _stale_file_result(error)
        read_ledger.invalidate(destination_path)
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("fs_copy", run=_fs_copy)

    def _fs_delete(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_workspace_relative_path(
            tool_name="fs_delete",
            raw_path=args.get("path"),
            raw_base=args.get("path_base"),
            field_name="path",
            base_field_name="path_base",
        )
        try:
            _guard_write_path(path)
        except AgentRuntimeError as exc:
            if (
                persona_write_scope_active
                or "outside allowed scope" not in str(exc)
                or not is_non_material_untracked_path(path)
            ):
                raise
        precondition = capture_file_precondition(root=root, path=path)
        sensitive = guard_sensitive_files("fs_delete", files=[path])
        preview = f"Delete file\npath: {path}"
        if not sensitive:
            guard_write("fs_delete", preview, files=[path])
        try:
            result = fs_delete(root=root, path=path, precondition=precondition)
        except StaleFileError as error:
            return _stale_file_result(error)
        read_ledger.invalidate(path)
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("fs_delete", run=_fs_delete)

    def _fs_write(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_workspace_relative_path(
            tool_name="fs_write",
            raw_path=args.get("path"),
            raw_base=args.get("path_base"),
            field_name="path",
            base_field_name="path_base",
        )
        _guard_write_path(path)
        content = str(args.get("content", ""))
        prepared = prepare_fs_write(root=root, path=path, content=content)
        sensitive = guard_sensitive_files("fs_write", files=[path])
        rewrite_warning: str | None = None
        if sensitive:
            store.append(
                "sensitive_change_preview",
                {"path": path, "operation": "fs_write", "content_redacted": True},
            )
        else:
            if prepared.precondition.exists:
                old = _patchable("fs_read", fs_read)(root=root, path=path, max_bytes=2_000_000)[
                    "content"
                ]
            else:
                old = ""
            if edit_discipline is not None and old:
                # Free: `old` is already in hand for the diff preview below, so
                # scoring the overwrite costs no I/O and never scans the
                # workspace. Advisory only -- the write proceeds either way, and
                # a guard that raised would be a guard that broke writes.
                try:
                    rewrite_warning = edit_discipline.warn_for_write(
                        path=path, original=old, updated=content
                    )
                except Exception:  # noqa: BLE001 - advice must never fail a write
                    rewrite_warning = None
            diff = _unified_diff(old, content, path)
            store.append("diff_preview", {"path": path, "diff": diff[:20000]})
            surface.on_patch_generated(
                PatchEvent(
                    files=[path],
                    diff=diff,
                    summary=f"1 file changed via fs_write ({path})",
                )
            )
            guard_write("fs_write", diff[:20000] or f"(no diff) {path}", files=[path])
        try:
            result = write_prepared_fs_write(prepared, root=root)
        except StaleFileError as error:
            return _stale_file_result(error)
        read_ledger.invalidate(path)
        if rewrite_warning is not None:
            # Attached only after the write succeeded, so the model never reads
            # advice about an overwrite that did not happen.
            result["warning"] = rewrite_warning
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("fs_write", run=_fs_write)

    def _fs_mkdir(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_workspace_relative_path(
            tool_name="fs_mkdir",
            raw_path=args.get("path"),
            raw_base=args.get("path_base"),
            field_name="path",
            base_field_name="path_base",
        )
        if _is_allowed_ancestor_dir_creation(path):
            if _is_denied_path(path):
                raise AgentRuntimeError(f"Blocked write to protected path: {path}")
        else:
            _guard_write_path(path)
        parents = bool(args.get("parents", True))
        exist_ok = bool(args.get("exist_ok", True))
        preview = (
            "Create directory\n"
            f"path: {path}\n"
            f"parents: {str(parents).lower()}\n"
            f"exist_ok: {str(exist_ok).lower()}"
        )
        guard_write("fs_mkdir", preview, files=[path])
        return fs_mkdir(
            root=root,
            path=path,
            parents=parents,
            exist_ok=exist_ok,
        )

    _append_builtin_tool("fs_mkdir", run=_fs_mkdir)

    if cfg is not None and cfg.image_generation.enabled:

        def _image_generate(args: dict[str, Any]) -> dict[str, Any]:
            try:
                count = int(args.get("count", 1))
                planned = plan_image_output_paths(
                    root=root,
                    output_path=str(args.get("output_path") or ""),
                    count=count,
                )
            except (ImageGenerationError, TypeError, ValueError) as exc:
                raise AgentRuntimeError(f"Invalid image generation request: {exc}") from exc
            relative_paths = [relative for _path, relative in planned]
            for relative_path in relative_paths:
                _guard_write_path(relative_path)
            preview = (
                "Generate image asset(s)\n"
                f"model: {cfg.image_generation.model}\n"
                f"paths: {', '.join(relative_paths)}\n"
                f"count: {count}\n"
                f"size: {str(args.get('size') or 'auto')}\n"
                f"quality: {str(args.get('quality') or 'auto')}\n"
                f"background: {str(args.get('background') or 'auto')}"
            )
            guard_write("image_generate", preview, files=relative_paths)
            try:
                result = generate_images(
                    root=root,
                    cfg=cfg,
                    fallback_api_key=api_key,
                    prompt=str(args.get("prompt") or ""),
                    output_path=str(args.get("output_path") or ""),
                    count=count,
                    size=str(args.get("size") or "auto"),
                    quality=str(args.get("quality") or "auto"),
                    background=str(args.get("background") or "auto"),
                    timeout_s=_deadline_timeout(
                        cfg.image_generation.timeout_s,
                        operation="image_generate",
                    ),
                )
            except (ImageGenerationError, DeadlineExhausted) as exc:
                raise AgentRuntimeError(f"Image generation failed: {exc}") from exc
            store.append("image_generated", dict(result))
            return result

        _append_builtin_tool("image_generate", run=_image_generate)

    _append_builtin_tool(
        "fs_list",
        run=lambda args: _patchable("fs_list", fs_list)(
            root=root,
            root_path=_resolve_workspace_relative_path(
                tool_name="fs_list",
                raw_path=args.get("root_path"),
                raw_base=args.get("path_base"),
                field_name="root_path",
                base_field_name="path_base",
                allow_empty=True,
            ),
            globs=args.get("globs"),
            ignore=args.get("ignore"),
        ),
    )

    def _managed_browser_cancelled() -> bool:
        if managed_browser_cancel_check is None:
            return False
        try:
            return bool(managed_browser_cancel_check())
        except Exception:  # noqa: BLE001 - a broken host token fails closed
            return True

    def _browser_public_url(raw_url: Any) -> str | None:
        value = str(raw_url or "").strip()
        if not value:
            return None
        try:
            split = urlsplit(value)
            scheme = split.scheme.lower()
            hostname = split.hostname
            if scheme not in {"http", "https"} or not hostname:
                return None
            host = f"[{hostname}]" if ":" in hostname else hostname
            if split.port is not None:
                host = f"{host}:{split.port}"
            public_url = urlunsplit((scheme, host, split.path or "/", "", ""))
        except (TypeError, ValueError):
            return None
        return str(redact_secrets(public_url))

    def _browser_status_payload(status: BrowserSessionStatus | Any) -> dict[str, Any]:
        if isinstance(status, dict):
            source = status
        else:
            source = {
                key: getattr(status, key, None)
                for key in (
                    "session_id",
                    "product",
                    "state",
                    "created_at",
                    "active_url",
                    "artifact_count",
                )
            }
        return {
            "session_id": str(source.get("session_id") or ""),
            "product": str(source.get("product") or ""),
            "state": str(source.get("state") or ""),
            "created_at": source.get("created_at"),
            "active_url": _browser_public_url(source.get("active_url")),
            "artifact_count": int(source.get("artifact_count") or 0),
        }

    def _browser_action_preview(kind: str, args: dict[str, Any]) -> str:
        session_id = str(args.get("session_id") or "").strip()
        lines = [f"Managed browser action: {kind}"]
        if session_id:
            lines.append(f"session: {session_id[:80]}")
        if kind == "browser_navigate":
            target = _browser_public_url(args.get("url"))
            lines.append(f"public target: {target or '(invalid URL)'}")
            lines.append("query parameters and fragments are omitted from this preview")
        elif kind in {"browser_click", "browser_type"}:
            selector = str(args.get("selector") or "")
            lines.append(f"selector: {selector[:300]}")
            if kind == "browser_type":
                lines.append(f"input characters: {len(str(args.get('text') or ''))}")
                lines.append("input text is intentionally omitted")
        elif kind == "browser_start":
            target = _browser_public_url(args.get("url"))
            if target:
                lines.append(f"initial target: {target}")
                lines.append("query parameters and fragments are omitted from this preview")
            lines.append("network policy: public or session-owned preview destinations")
        return "\n".join(lines)

    def _guard_managed_browser_action(kind: str, args: dict[str, Any]) -> None:
        if not host_managed_approvals:
            raise AgentRuntimeError(
                "Managed browser state changes require an IDE host-managed approval."
            )
        decision = surface.request_approval(
            ApprovalRequest(
                kind=kind,
                reason="managed browser actions can launch processes or change remote page state",
                preview=_browser_action_preview(kind, args),
                command=kind,
                metadata={
                    "managed_browser": True,
                    "mandatory_explicit_approval": True,
                    "allow_for_session_disabled": True,
                    "public_or_owned_preview_destinations": True,
                },
                allow_for_session_scope=None,
            )
        )
        if not decision.allow:
            raise ApprovalDeclinedError(kind)
        if decision.allow_for_session:
            raise AgentRuntimeError(
                "Managed browser state changes require a one-time host approval for each action."
            )

    def _run_managed_browser(operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except BrowserError as exc:
            raise AgentRuntimeError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - do not expose provider or page secrets
            raise AgentRuntimeError("Managed browser operation failed safely.") from exc

    allowed_child_browser_tools = {
        str(name or "").strip() for name in (child_managed_browser_tool_names or ())
    }
    browser_tools_enabled = managed_browser_service is not None and (
        subagent_depth == 0 or (subagent_depth == 1 and bool(allowed_child_browser_tools))
    )

    def _session_owned_preview_urls() -> tuple[str, ...]:
        if durable_service_manager is None:
            return ()
        try:
            active_services = durable_service_manager.list_active()
        except Exception:
            return ()
        urls: list[str] = []
        for service in active_services:
            if not isinstance(service, dict):
                continue
            candidates = [service.get("preview_url")]
            raw_urls = service.get("preview_urls")
            if isinstance(raw_urls, list):
                candidates.extend(raw_urls)
            for raw in candidates:
                value = str(raw or "").strip()
                if value and value not in urls:
                    urls.append(value)
                if len(urls) >= 64:
                    return tuple(urls)
        return tuple(urls)

    if browser_tools_enabled:
        browser_owner_id = str(managed_browser_owner_id or store.session_id).strip()

        def _browser_is_direct_only(status: BrowserSessionStatus | Any) -> bool:
            if isinstance(status, dict):
                return bool(status.get("allow_local_destinations"))
            return bool(getattr(status, "allow_local_destinations", False))

        def _browser_require_agent_visible(session_id: str) -> BrowserSessionStatus | Any:
            """Fence direct localhost sessions away from model-controlled tools.

            ``allow_local_destinations`` is immutable for a managed-browser
            session, so checking the owner-scoped status before every action is
            a stable actor boundary rather than a best-effort URL check.
            """

            status = _run_managed_browser(
                lambda: managed_browser_service.status(browser_owner_id, session_id)
            )
            if _browser_is_direct_only(status):
                raise AgentRuntimeError(
                    "This browser session is reserved for direct IDE localhost testing."
                )
            return status

        def _browser_start(args: dict[str, Any]) -> dict[str, Any]:
            _guard_managed_browser_action("browser_start", args)
            status = _run_managed_browser(
                lambda: managed_browser_service.start(
                    browser_owner_id,
                    allow_local_destinations=False,
                    allowed_preview_urls_provider=_session_owned_preview_urls,
                    cancel=_managed_browser_cancelled,
                )
            )
            payload = _browser_status_payload(status)
            target = str(args.get("url") or "").strip()
            if not target:
                return payload
            session_id = str(payload.get("session_id") or "")
            try:
                navigated = _run_managed_browser(
                    lambda: managed_browser_service.navigate(
                        browser_owner_id,
                        session_id,
                        target,
                        timeout=args.get("timeout"),
                        cancel=_managed_browser_cancelled,
                    )
                )
            except Exception:
                try:
                    managed_browser_service.close(
                        browser_owner_id,
                        session_id,
                        delete_artifacts=True,
                    )
                except Exception:
                    pass
                raise
            navigated_payload = dict(navigated) if isinstance(navigated, dict) else {}
            payload["active_url"] = _browser_public_url(navigated_payload.get("url") or target)
            return payload

        def _browser_navigate(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            _guard_managed_browser_action("browser_navigate", args)
            result = _run_managed_browser(
                lambda: managed_browser_service.navigate(
                    browser_owner_id,
                    session_id,
                    str(args.get("url") or ""),
                    timeout=args.get("timeout"),
                    cancel=_managed_browser_cancelled,
                )
            )
            payload = dict(result) if isinstance(result, dict) else {}
            payload["url"] = _browser_public_url(payload.get("url") or args.get("url"))
            return payload

        def _browser_snapshot(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            result = _run_managed_browser(
                lambda: managed_browser_service.snapshot(
                    browser_owner_id,
                    session_id,
                    kind=str(args.get("kind") or "semantic"),
                    timeout=args.get("timeout"),
                    cancel=_managed_browser_cancelled,
                )
            )
            return dict(result) if isinstance(result, dict) else {"data": result}

        def _browser_screenshot(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            artifact = _run_managed_browser(
                lambda: managed_browser_service.screenshot(
                    browser_owner_id,
                    session_id,
                    full_page=bool(args.get("full_page", False)),
                    timeout=args.get("timeout"),
                    cancel=_managed_browser_cancelled,
                )
            )
            if not isinstance(artifact, BrowserArtifact) and not hasattr(artifact, "artifact_id"):
                raise AgentRuntimeError("Managed browser returned an invalid artifact.")
            return {
                "artifact_id": str(artifact.artifact_id),
                "media_type": str(artifact.media_type),
                "size_bytes": int(artifact.size_bytes),
                "sha256": str(artifact.sha256),
            }

        def _browser_artifact_read(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            result = _run_managed_browser(
                lambda: managed_browser_service.read_artifact(
                    browser_owner_id,
                    session_id,
                    str(args.get("artifact_id") or ""),
                    offset=args.get("offset", 0),
                    max_bytes=args.get("max_bytes", 256 * 1024),
                )
            )
            return dict(result) if isinstance(result, dict) else {"data": result}

        def _browser_diagnostics(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            result = _run_managed_browser(
                lambda: managed_browser_service.diagnostics(
                    browser_owner_id,
                    session_id,
                    max_events=args.get("max_events"),
                    timeout=args.get("timeout"),
                    cancel=_managed_browser_cancelled,
                )
            )
            return dict(result) if isinstance(result, dict) else {"data": result}

        def _browser_click(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            _guard_managed_browser_action("browser_click", args)
            result = _run_managed_browser(
                lambda: managed_browser_service.click(
                    browser_owner_id,
                    session_id,
                    str(args.get("selector") or ""),
                    timeout=args.get("timeout"),
                    cancel=_managed_browser_cancelled,
                )
            )
            return dict(result) if isinstance(result, dict) else {"clicked": bool(result)}

        def _browser_type(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            _guard_managed_browser_action("browser_type", args)
            result = _run_managed_browser(
                lambda: managed_browser_service.type_text(
                    browser_owner_id,
                    session_id,
                    str(args.get("selector") or ""),
                    str(args.get("text") or ""),
                    replace=bool(args.get("replace", True)),
                    timeout=args.get("timeout"),
                    cancel=_managed_browser_cancelled,
                )
            )
            payload = dict(result) if isinstance(result, dict) else {"typed": bool(result)}
            payload.pop("text", None)
            return payload

        def _browser_status(args: dict[str, Any]) -> dict[str, Any]:
            status = _browser_require_agent_visible(str(args.get("session_id") or ""))
            return _browser_status_payload(status)

        def _browser_list(_args: dict[str, Any]) -> dict[str, Any]:
            statuses = _run_managed_browser(lambda: managed_browser_service.list(browser_owner_id))
            items = [
                _browser_status_payload(status)
                for status in tuple(statuses)
                if not _browser_is_direct_only(status)
            ]
            return {"sessions": items, "count": len(items)}

        def _browser_close(args: dict[str, Any]) -> dict[str, Any]:
            session_id = str(args.get("session_id") or "")
            _browser_require_agent_visible(session_id)
            _guard_managed_browser_action("browser_close", args)
            closed = _run_managed_browser(
                lambda: managed_browser_service.close(
                    browser_owner_id,
                    session_id,
                    # Browser screenshots are ephemeral owner-scoped artifacts.
                    # The service has no safe retained-session read surface, so
                    # close must not leave unreachable private files behind.
                    delete_artifacts=True,
                )
            )
            return {"session_id": session_id, "closed": bool(closed)}

        def _append_browser_tool(
            name: str,
            *,
            run: Callable[[dict[str, Any]], dict[str, Any]],
        ) -> None:
            if subagent_depth == 1 and name not in allowed_child_browser_tools:
                return
            _append_builtin_tool(name, run=run)

        _append_browser_tool("browser_start", run=_browser_start)
        _append_browser_tool("browser_navigate", run=_browser_navigate)
        _append_browser_tool("browser_snapshot", run=_browser_snapshot)
        _append_browser_tool("browser_screenshot", run=_browser_screenshot)
        _append_browser_tool("browser_artifact_read", run=_browser_artifact_read)
        _append_browser_tool("browser_diagnostics", run=_browser_diagnostics)
        _append_browser_tool("browser_click", run=_browser_click)
        _append_browser_tool("browser_type", run=_browser_type)
        _append_browser_tool("browser_status", run=_browser_status)
        _append_browser_tool("browser_list", run=_browser_list)
        _append_browser_tool("browser_close", run=_browser_close)

    # Master web-tools switch: when off (config field or ALYSIS_WEB_TOOLS env),
    # neither web_fetch nor web_search is registered at all — the model never sees
    # them in its tool list. Required for benchmark/offline integrity.
    web_tools_enabled = resolve_web_tools_enabled(cfg)

    web_search_exposed_in_mode = (
        web_tools_enabled
        and _built_in_tool_exposed_in_mode(
            tool_name="web_search",
            mode=mode,
            subagent_depth=subagent_depth,
            readonly_child_web_tool_names=readonly_child_web_tool_names,
        )
        and resolve_web_search_policy(cfg) != "off"
    )
    web_search_status = (
        resolve_web_search_runtime_status(cfg=cfg, api_key=api_key)
        if web_search_exposed_in_mode
        else None
    )

    def _web_fetch_recovery_is_public_candidate(raw_url: str) -> bool:
        normalized = normalize_web_url(raw_url)
        if normalized is None:
            return False
        try:
            split = urlsplit(normalized)
        except ValueError:
            return False
        host = (split.hostname or "").rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(".localhost"):
            return False
        if split.username is not None or split.password is not None:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        )

    def _web_fetch_source_matches_requested(*, source_url: str, requested_url: str) -> bool:
        source = normalize_web_url(source_url)
        requested = normalize_web_url(requested_url)
        if source is None or requested is None:
            return False
        if source == requested:
            return True
        source_split = urlsplit(source)
        requested_split = urlsplit(requested)
        if (
            source_split.scheme,
            source_split.netloc,
            source_split.query,
        ) != (
            requested_split.scheme,
            requested_split.netloc,
            requested_split.query,
        ):
            return False
        return source_split.path.rstrip("/") == requested_split.path.rstrip("/")

    def _web_fetch_recovery_display_url(raw_url: str) -> str:
        normalized = normalize_web_url(raw_url)
        if normalized is not None:
            return normalized
        canonical = canonicalize_web_url_input(raw_url)
        if canonical is not None:
            return canonical
        try:
            split = urlsplit(str(raw_url or "").strip())
            port = split.port
        except ValueError:
            return "[invalid URL omitted]"
        scheme = str(split.scheme or "").lower()
        host = (split.hostname or "").rstrip(".").lower()
        if scheme not in {"http", "https"} or not host:
            return "[unsupported URL omitted]"
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None
        netloc = host if port is None else f"{host}:{port}"
        path = split.path or "/"
        return urlunsplit((scheme, netloc, path, split.query, ""))

    def _web_fetch_recovery_payload(
        *,
        requested_url: str,
        raw_requested_url: str,
        finalization_suppressed: bool,
        automatic_attempted: bool = False,
        search_error: str = "",
    ) -> dict[str, Any]:
        display_url = _web_fetch_recovery_display_url(requested_url)
        query = build_web_fetch_recovery_search_query(display_url)
        payload: dict[str, Any] = {
            "error": (
                "web_fetch only allows a URL explicitly provided by the user or one returned "
                "by web_search earlier in this session."
            ),
            "error_code": "web_fetch_provenance_required",
            "url": display_url,
            "allowed_provenance": [
                "user_provided",
                "returned_by_web_search",
                "fetched_page_link",
                "trusted_local_file",
                "trusted_tool_output",
                "canonical_redirect",
                "search_mediated_recovery",
                "same_origin_derived_search_result",
            ],
            "provenance_recovery": {
                "suggested_search_query": query,
                "web_search_available": bool(
                    web_search_status is not None and web_search_status.registration_ready
                ),
                "automatic_recovery_attempted": automatic_attempted,
                "finalization_suppressed": finalization_suppressed,
                "search_error": search_error,
            },
        }
        canonical_raw = normalize_web_url(raw_requested_url)
        if canonical_raw is not None and canonical_raw != display_url:
            payload["raw_input_url"] = raw_requested_url
        return payload

    def _maybe_establish_web_fetch_provenance_via_search(
        *,
        requested_url: str,
        raw_requested_url: str,
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        finalization_suppressed = (
            execution_deadline is not None
            and execution_deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW
        )
        base_payload = _web_fetch_recovery_payload(
            requested_url=requested_url,
            raw_requested_url=raw_requested_url,
            finalization_suppressed=finalization_suppressed,
        )
        if finalization_suppressed:
            return None, None, base_payload
        if web_search_status is None or not web_search_status.registration_ready:
            return None, None, base_payload
        if not _web_fetch_recovery_is_public_candidate(requested_url):
            return None, None, base_payload
        query = str(base_payload["provenance_recovery"]["suggested_search_query"] or "").strip()
        if not query:
            return None, None, base_payload
        try:
            host = (
                urlsplit(normalize_web_url(requested_url) or requested_url).hostname or ""
            ).lower()
            search_result = web_search(
                query=query,
                cfg=cfg,
                api_key=api_key,
                allowed_domains=[host] if host else None,
                max_sources=5,
                external_web_access=True,
                session_id=str(getattr(store, "session_id", "") or "") or None,
            )
        except WebSearchError as exc:
            return (
                None,
                None,
                _web_fetch_recovery_payload(
                    requested_url=requested_url,
                    raw_requested_url=raw_requested_url,
                    finalization_suppressed=finalization_suppressed,
                    automatic_attempted=True,
                    search_error=str(exc),
                ),
            )
        matching_source_url = ""
        for source in list(search_result.get("sources") or []):
            if not isinstance(source, dict):
                continue
            source_url = str(source.get("url") or "").strip()
            if _web_fetch_source_matches_requested(
                source_url=source_url,
                requested_url=requested_url,
            ):
                matching_source_url = source_url
                break
        if not matching_source_url:
            payload = _web_fetch_recovery_payload(
                requested_url=requested_url,
                raw_requested_url=raw_requested_url,
                finalization_suppressed=finalization_suppressed,
                automatic_attempted=True,
            )
            payload["provenance_recovery"]["search_result_source_count"] = len(
                list(search_result.get("sources") or [])
            )
            return None, None, payload
        _changed, normalized = store.establish_search_mediated_web_fetch_url(
            raw_url=requested_url,
            query=query,
            source_url=matching_source_url,
        )
        store.append(
            "web_fetch_provenance_recovery",
            {
                "url": requested_url,
                "normalized_url": normalized,
                "query": query,
                "source_url": matching_source_url,
                "provenance_classification": "search_mediated_recovery",
            },
        )
        return store.resolve_web_fetch_url(requested_url)[0], normalized, None

    def _web_fetch_tool(args: dict[str, Any]) -> dict[str, Any]:
        raw_requested_url = str(args.get("url", "")).strip()
        provenance_classification, resolved_requested_url = store.resolve_web_fetch_url(
            raw_requested_url
        )
        requested_url = resolved_requested_url or raw_requested_url
        recovered_via_search = False
        if provenance_classification is None:
            (
                provenance_classification,
                recovered_url,
                recovery_result,
            ) = _maybe_establish_web_fetch_provenance_via_search(
                requested_url=requested_url,
                raw_requested_url=raw_requested_url,
            )
            if provenance_classification is None:
                rejection = recovery_result or _web_fetch_recovery_payload(
                    requested_url=requested_url,
                    raw_requested_url=raw_requested_url,
                    finalization_suppressed=False,
                )
                # Make the rejection self-correcting: tell the model exactly which
                # URLs it MAY fetch (prior trusted session evidence) so it retries
                # against a real source instead of a guessed/restated one — without
                # widening what is authorized.
                fetchable_urls = store.fetchable_web_fetch_urls()
                if fetchable_urls:
                    rejection["fetchable_urls"] = fetchable_urls
                    rejection["guidance"] = (
                        "Do not guess or restate URLs from memory. Retry web_fetch with one of "
                        "fetchable_urls (these came from prior trusted session evidence), or run "
                        "web_search again to find the page."
                    )
                else:
                    rejection["guidance"] = (
                        "No URLs are fetchable yet. Run web_search first, or ask the user for "
                        "the exact URL. Do not guess URLs."
                    )
                return rejection
            recovered_via_search = True
            if recovered_url:
                requested_url = recovered_url
        result = _patchable("web_fetch", web_fetch)(
            url=requested_url,
            max_chars=(args["max_chars"] if "max_chars" in args else 20000),
        )
        if recovered_via_search:
            result["provenance_classification"] = provenance_classification
        return result

    if web_tools_enabled:
        _append_builtin_tool("web_fetch", run=_web_fetch_tool)

    if web_search_exposed_in_mode and web_search_status is not None:
        if (
            emit_web_search_runtime_diagnostics
            and web_search_status.mode == "auto"
            and not web_search_status.registration_ready
        ):
            store.append("web_search_runtime_unavailable", web_search_status.to_payload())

        if web_search_status.registration_ready:
            _append_builtin_tool(
                "web_search",
                run=lambda args: web_search(
                    query=str(args.get("query", "")),
                    cfg=cfg,
                    api_key=api_key,
                    allowed_domains=args.get("allowed_domains"),
                    max_sources=(args["max_sources"] if "max_sources" in args else 8),
                    external_web_access=(
                        args["external_web_access"] if "external_web_access" in args else True
                    ),
                    session_id=str(getattr(store, "session_id", "") or "") or None,
                ),
            )

    _append_builtin_tool(
        "symbol_search",
        run=lambda args: _patchable("symbol_search", symbol_search)(
            root=root,
            query=str(args.get("query", "")),
            kind=str(args["kind"]) if args.get("kind") is not None else None,
            root_path=_resolve_workspace_relative_path(
                tool_name="symbol_search",
                raw_path=args.get("root_path"),
                raw_base=args.get("path_base"),
                field_name="root_path",
                base_field_name="path_base",
                allow_empty=True,
            ),
            globs=args.get("globs"),
            max_results=(int(args["max_results"]) if args.get("max_results") is not None else 100),
            exact=bool(args.get("exact", False)),
            include_details=bool(args.get("include_details", False)),
            include_snippet=bool(args.get("include_snippet", False)),
            include_references=bool(args.get("include_references", False)),
        ),
    )

    _append_builtin_tool(
        "test_discover",
        run=lambda args: _patchable("test_discover", test_discover)(
            root=root,
            paths=args.get("paths"),
            symbols=args.get("symbols"),
            changed_only=bool(args.get("changed_only", False)),
            include_commands=bool(args.get("include_commands", True)),
            max_results=(int(args["max_results"]) if args.get("max_results") is not None else 20),
            failure_summary=(
                args.get("failure_summary")
                if isinstance(args.get("failure_summary"), dict)
                else None
            ),
        ),
    )

    _append_builtin_tool(
        "repo_map",
        run=lambda args: _patchable("repo_map", repo_map)(
            root=root,
            paths=args.get("paths"),
            symbols=args.get("symbols"),
            include_tests=bool(args.get("include_tests", True)),
            include_imports=bool(args.get("include_imports", True)),
            include_references=bool(args.get("include_references", False)),
            depth=(int(args["depth"]) if args.get("depth") is not None else 2),
            max_items=(int(args["max_items"]) if args.get("max_items") is not None else 80),
        ),
    )

    _append_builtin_tool(
        "search_rg",
        run=lambda args: _patchable("search_rg", search_rg)(
            root=root,
            pattern=str(args.get("pattern", "")),
            root_path=_resolve_workspace_relative_path(
                tool_name="search_rg",
                raw_path=args.get("root_path"),
                raw_base=args.get("path_base"),
                field_name="root_path",
                base_field_name="path_base",
                allow_empty=True,
            ),
            globs=args.get("globs"),
            before_context=(
                int(args["before_context"]) if args.get("before_context") is not None else 0
            ),
            after_context=(
                int(args["after_context"]) if args.get("after_context") is not None else 0
            ),
            literal=bool(args.get("literal", False)),
            case_sensitive=bool(args.get("case_sensitive", True)),
            include_hidden=bool(args.get("include_hidden", False)),
            max_results=(int(args["max_results"]) if args.get("max_results") is not None else 200),
        ),
    )

    if history_artifact_persistence_available:

        def _session_artifact_read(args: dict[str, Any]) -> dict[str, Any]:
            locator = str(args.get("locator", ""))
            try:
                return session_artifact_read(
                    artifact_layout=store.session_artifact_layout,
                    locator=locator,
                    max_bytes=args.get("max_bytes"),
                    offset=args.get("offset"),
                )
            except SessionArtifactReadError as exc:
                payload = getattr(exc, "result_payload", None)
                if (
                    isinstance(payload, dict)
                    and payload.get("error_code") == "session_artifact_session_mismatch"
                ):
                    store.append(
                        "session_artifact_read_session_mismatch",
                        {
                            "locator": locator,
                            "runtime_kind": resolved_runtime_kind.value,
                            "terminal": True,
                        },
                    )
                raise

        _append_builtin_tool(
            "session_artifact_read",
            run=_session_artifact_read,
        )
        _append_builtin_tool(
            "history_search",
            run=lambda args: history_search(
                root=root,
                session_id=store.session_id,
                session_artifact_root=store.session_artifact_root,
                pattern=str(args.get("pattern", "")),
                max_results=int(args.get("max_results") or 50),
                max_file_bytes=int(args.get("max_file_bytes") or 200000),
                include_history=bool(args.get("include_history", True)),
                include_tool_outputs=bool(args.get("include_tool_outputs", True)),
                include_memory=bool(args.get("include_memory", True)),
            ),
        )

    if skills_enabled and resolved_skill_registry:

        def _skill_read(args: dict[str, Any]) -> dict[str, Any]:
            raw_name = str(args.get("name", "")).strip()
            if not raw_name:
                return {"error": "Missing required argument: name"}
            skill = resolve_skill_by_name(resolved_skill_registry, raw_name)
            if skill is None:
                return {
                    "error": f"Unknown skill: {raw_name}",
                    "available_skills": sorted(
                        skill.name for skill in resolved_skill_registry.values()
                    ),
                }
            try:
                return read_skill_bundle_file(
                    skill,
                    path=(str(args["path"]) if args.get("path") is not None else None),
                )
            except SkillReadError as exc:
                return {
                    "error": str(exc),
                    "name": skill.name,
                    "source_path": skill.source_path.as_posix(),
                }

        _append_builtin_tool("skill_read", run=_skill_read)

    verify_artifact_counter = 0

    def _next_verify_artifact_path() -> Path:
        nonlocal verify_artifact_counter
        verify_artifact_counter += 1
        return store.runtime_artifact_path(
            "verify",
            f"step{verify_artifact_counter:03d}_verify_run.txt",
        )

    def _workspace_services_before_verification() -> list[dict[str, Any]]:
        """Alysis Code-managed processes already alive in this workspace.

        Reported, never acted on. A dev server holding a port or writing into
        the same build directory is a common source of confusing verification
        output, and the host cannot tell a genuine conflict from a deliberate
        setup. The agent decides whether it matters and stops anything through
        the normal approval path.
        """

        services: list[dict[str, Any]] = []
        if terminal_manager is not None:
            try:
                for summary in terminal_manager.list():
                    if summary.status != "running":
                        continue
                    services.append(
                        {
                            "kind": "background_process",
                            "process_id": summary.process_id,
                            "command": summary.cmd,
                            "cwd": str(summary.cwd),
                            "runtime_s": round(float(summary.runtime_s), 1),
                        }
                    )
            except Exception:  # noqa: BLE001 - reporting must never fail verification
                pass
        if durable_service_manager is not None:
            try:
                for entry in durable_service_manager.list_active():
                    services.append(
                        {
                            "kind": "durable_service",
                            "service_id": str(entry.get("service_id") or ""),
                            "command": str(entry.get("command") or ""),
                            "url": str(entry.get("url") or ""),
                        }
                    )
            except Exception:  # noqa: BLE001 - reporting must never fail verification
                pass
        return services

    def _verify_run(args: dict[str, Any]) -> dict[str, Any]:
        deadline_decision = _deadline_start_decision(
            DeadlineOperation.VERIFICATION,
            minimum_remaining_seconds=MINIMUM_TOOL_START_SECONDS,
            allow_during_finalization=True,
        )
        if deadline_decision is not None and not bool(deadline_decision.get("allowed")):
            return _deadline_error(
                "verify_run skipped because the run deadline is exhausted or too close.",
                start_decision=deadline_decision,
            )
        try:
            verify_timeout_s = _deadline_timeout(900, operation="verify_run")
        except DeadlineExhausted:
            return _deadline_error(
                "verify_run skipped because the run deadline is exhausted or too close."
            )
        effective_cfg = cfg or AppConfig(model="")
        raw_commands = args.get("commands")
        verify_cmd: list[str] | None = None
        current_selection = _current_verify_selection()
        current_effective_verification_commands = _normalized_verify_commands(
            list(current_selection.commands) if current_selection is not None else []
        )
        unavailable_verification_contract = bool(
            current_selection is not None
            and str(current_selection.contract_type or "").strip() == "unavailable"
            and not current_effective_verification_commands
        )
        ignore_explicit_commands_for_unavailable_contract = (
            unavailable_verification_contract
            and authoritative_verify_commands is None
            and (
                (not one_shot_execution and resolved_runtime_kind == RuntimeKind.INTERACTIVE_CHAT)
                or not _normalized_verify_commands(
                    getattr(effective_cfg, "verify_commands", []) or []
                )
            )
        )
        trusted_shell_commands = trusted_shell_expression_command_set(current_selection)

        def _validate_explicit_verify_candidate(command: str) -> None:
            normalized_exact = " ".join(str(command or "").strip().split())
            trusted = normalized_exact in trusted_shell_commands
            analysis = analyze_verification_command(
                command,
                trusted=trusted,
                workspace_root=root,
            )
            if analysis.rejection_reason:
                raise VerifyError("verification command is invalid: " + analysis.rejection_reason)
            if _has_disallowed_shell_control_flow(command) and not trusted:
                raise VerifyError("verification command is invalid: disallowed_shell_control_flow")

        selection_metadata = verification_selection_payload(
            current_selection
            if current_selection is not None
            else ResolvedVerifyCommands(
                commands=tuple(current_effective_verification_commands),
                source="session.effective_verification_commands",
                reason="session already resolved an effective verification contract",
                contract_type="selected",
            ),
            authoritative=(
                is_authoritative_verify_command_selection(current_selection)
                if current_selection is not None
                else bool(authoritative_verify_commands is not None)
            ),
        )
        selection_metadata.update(verification_command_specs_payload(current_selection))
        if raw_commands is not None:
            if not isinstance(raw_commands, list):
                raise VerifyError("commands must be an array of command strings.")
            verify_cmd = []
            for item in raw_commands:
                text = str(item).strip()
                if not text:
                    raise VerifyError("commands cannot contain empty values.")
                expanded_commands = _expand_simple_verify_command_chain(
                    text,
                    workspace_root=root,
                )
                if not ignore_explicit_commands_for_unavailable_contract:
                    if len(expanded_commands) == 1 and expanded_commands[0] == text:
                        _validate_explicit_verify_candidate(text)
                    else:
                        for command in expanded_commands:
                            _validate_explicit_verify_candidate(command)
                verify_cmd.extend(expanded_commands)
            if not verify_cmd:
                raise VerifyError("commands cannot be empty.")

        ignored_model_verification_commands: list[str] = []
        if authoritative_verify_commands is not None:
            if verify_cmd is not None:
                requested_commands = _normalized_verify_commands(verify_cmd)
                if requested_commands != authoritative_verify_commands:
                    raise VerifyError(
                        "Managed verification commands are locked to the authoritative Forge command set."
                    )
            commands = list(authoritative_verify_commands)
        elif verify_cmd is not None and current_effective_verification_commands:
            requested_commands = _normalized_verify_commands(verify_cmd)
            incompatible_commands = _verify_run_commands_match_effective_contract(
                requested_commands=requested_commands,
                effective_verification_commands=current_effective_verification_commands,
            )
            if incompatible_commands:
                raise VerifyError(
                    "verify_run commands must stay within the session's effective verification contract."
                )
            commands = requested_commands
        elif verify_cmd is not None and unavailable_verification_contract:
            requested_commands = _normalized_verify_commands(verify_cmd)
            commands = []
            for command in requested_commands:
                analysis = analyze_verification_command(command, trusted=False, workspace_root=root)
                if (
                    analysis.evidentiary_capability
                    == VerificationCommandEvidentiaryCapability.ASSERTIVE
                    and not analysis.rejection_reason
                ):
                    commands.append(command)
                else:
                    ignored_model_verification_commands.append(command)
        elif verify_cmd is not None and current_selection is not None:
            commands = _normalized_verify_commands(verify_cmd)
        elif verify_cmd is None and current_selection is not None:
            commands = list(current_effective_verification_commands)
        else:
            commands = resolve_verify_commands(
                cfg=effective_cfg,
                verify_cmd=verify_cmd,
            )
        validation_errors = validation_errors_for_selection(current_selection)
        if validation_errors and (
            authoritative_verify_commands is not None
            or (
                current_selection is not None
                and is_authoritative_verify_command_selection(current_selection)
            )
        ):
            raise VerifyError(
                "authoritative verification command is invalid: " + "; ".join(validation_errors[:3])
            )
        for command in commands:
            normalized_exact = " ".join(str(command or "").strip().split())
            analysis = analyze_verification_command(
                command,
                trusted=normalized_exact in trusted_shell_commands,
                workspace_root=root,
            )
            if analysis.rejection_reason:
                raise VerifyError("verification command is invalid: " + analysis.rejection_reason)
            if (
                _has_disallowed_shell_control_flow(command)
                and normalized_exact not in trusted_shell_commands
            ):
                raise VerifyError(
                    "verification commands must be single commands without shell control flow or chaining."
                )
        guard_verify(commands)
        workspace_services = _workspace_services_before_verification()
        artifact_path = _next_verify_artifact_path()
        result, touched_repo_paths = _run_with_command_mutation_detection(
            root=root,
            enabled=command_mutation_tracking_enabled,
            ignored_paths=command_mutation_ignored_paths,
            operation=lambda: _call_with_optional_kwargs(
                _patchable("run_task_verification", run_task_verification),
                required_kwargs={
                    "root": root,
                    "commands": commands,
                    "artifact_path": artifact_path,
                    "cfg": effective_cfg,
                },
                optional_kwargs={
                    "timeout_s": verify_timeout_s,
                    "process_group_registry": process_group_registry,
                },
            ),
        )
        payload = verify_run_result_to_payload(root=root, result=result)
        if workspace_services:
            payload = dict(payload)
            payload["workspace_services"] = workspace_services
        if ignored_model_verification_commands:
            payload["ignored_model_verification_commands"] = ignored_model_verification_commands
            payload["verification_skip_reason"] = "verification_contract_unavailable"
        material_touched_repo_paths: list[str] = []
        if touched_repo_paths:
            payload = dict(payload)
            payload["touched_repo_paths"] = touched_repo_paths
            mutation_metadata = _command_mutation_metadata(
                root=root,
                touched_repo_paths=touched_repo_paths,
                command_was_verification=True,
            )
            payload.update(mutation_metadata)
            material_touched_repo_paths = list(
                mutation_metadata.get("material_touched_repo_paths") or []
            )
        verification_relevant_material_touched_paths = _verification_relevant_material_paths(
            material_touched_repo_paths
        )
        evidence_records: list[VerificationEvidence] = []
        command_results = payload.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or item.get("effective_command") or "")
                if not command:
                    continue
                exit_code_raw = item.get("exit_code")
                evidence_records.append(
                    classify_verification_evidence(
                        command,
                        known_verification_commands=current_effective_verification_commands,
                        authoritative=bool(selection_metadata.get("verification_authoritative")),
                        material_touched_paths=verification_relevant_material_touched_paths,
                        exit_code=(exit_code_raw if isinstance(exit_code_raw, int) else None),
                        output=str(item.get("output_preview") or ""),
                        real_execution=(
                            item.get("real_execution")
                            if isinstance(item.get("real_execution"), bool)
                            or item.get("real_execution") is None
                            else None
                        ),
                        root=root,
                    )
                )
        payload.update(_aggregate_tool_evidence_payload(evidence_records))
        payload.update(selection_metadata)
        stored_artifact_path = (
            os.fspath(result.artifact_path.resolve())
            if result.artifact_path.exists()
            else os.fspath(result.artifact_path)
        )
        store.append(
            "verify_run",
            {
                "commands": commands,
                "all_passed": result.all_passed,
                "summary": result.summary,
                "fallback_used": payload.get("fallback_used"),
                "fallback_count": payload.get("fallback_count"),
                "fallback_details": payload.get("fallback_details"),
                "artifact_path": stored_artifact_path,
                "model_artifact_path": payload.get("artifact_path"),
                "artifact_saved": payload.get("artifact_saved"),
                "artifact_readable_via_fs": payload.get("artifact_readable_via_fs"),
                "artifact_location": payload.get("artifact_location"),
                "verification_evidence_category": payload.get("verification_evidence_category"),
                "verification_evidence_reason": payload.get("verification_evidence_reason"),
                "verification_evidence_allowed": payload.get("verification_evidence_allowed"),
                "verification_evidence_supplemental_only": payload.get(
                    "verification_evidence_supplemental_only"
                ),
                "ignored_model_verification_commands": payload.get(
                    "ignored_model_verification_commands", []
                ),
                "verification_skip_reason": payload.get("verification_skip_reason"),
                "material_touched_repo_paths": payload.get("material_touched_repo_paths", []),
                "benign_runtime_paths": payload.get("benign_runtime_paths", []),
                **selection_metadata,
            },
        )
        return payload

    if verification_enabled:
        _append_builtin_tool("verify_run", run=_verify_run)

    def _shell(args: dict[str, Any]) -> dict[str, Any]:
        dispatch_started = perf_counter()
        deadline_decision = _deadline_start_decision(
            DeadlineOperation.SHELL_TOOL,
            minimum_remaining_seconds=MINIMUM_TOOL_START_SECONDS,
            allow_during_finalization=True,
        )
        if deadline_decision is not None and not bool(deadline_decision.get("allowed")):
            return _deadline_error(
                "shell_run skipped because the run deadline is exhausted or too close.",
                start_decision=deadline_decision,
            )
        try:
            shell_timeout_s = _deadline_timeout(60, operation="shell_run")
        except DeadlineExhausted:
            return _deadline_error(
                "shell_run skipped because the run deadline is exhausted or too close."
            )
        cmd = str(args.get("cmd", ""))
        effective_cwd = _resolve_workspace_relative_path(
            tool_name="shell_run",
            raw_path=args.get("cwd"),
            raw_base=args.get("cwd_base"),
            field_name="cwd",
            base_field_name="cwd_base",
            allow_empty=True,
        )
        guard_shell(cmd)
        store.append("cmd", {"cmd": cmd, "cwd": effective_cwd})
        started = perf_counter()
        result: dict[str, Any] | None = None
        command_seconds = 0.0

        def _run_shell_command() -> Any:
            # Timed on its own so that the workspace walks mutation detection
            # performs on either side of it are attributable to the dispatch
            # path rather than to the command the model asked for.
            nonlocal command_seconds
            command_started = perf_counter()
            try:
                return _call_with_optional_kwargs(
                    _patchable("shell_run", shell_run),
                    required_kwargs={
                        "root": root,
                        "cmd": cmd,
                        "cwd": effective_cwd,
                        "runner": shell_runner,
                    },
                    optional_kwargs={
                        "timeout_s": shell_timeout_s,
                        "capture_pipeline_status": _evidence_v2_enabled(cfg),
                    },
                )
            finally:
                command_seconds += perf_counter() - command_started

        try:
            result, touched_repo_paths = _run_with_command_mutation_detection(
                root=root,
                enabled=command_mutation_tracking_enabled,
                ignored_paths=command_mutation_ignored_paths,
                operation=_run_shell_command,
            )
        finally:
            if is_full_access_mode:
                duration_ms = int((perf_counter() - started) * 1000)
                store.append(
                    "fullaccess_shell",
                    {
                        "event": "fullaccess_shell",
                        "ts": _fullaccess_shell_audit_ts(),
                        "command": cmd,
                        "cwd": str((result or {}).get("cwd") or effective_cwd or root),
                        "exit_code": int((result or {}).get("exit_code", -1)),
                        "pipeline_stage_status": (result or {}).get("pipeline_stage_status"),
                        "duration_ms": duration_ms,
                        "mode": "fullaccess",
                    },
                )
        if touched_repo_paths:
            result = dict(result)
            result["touched_repo_paths"] = touched_repo_paths
            result.update(
                _command_mutation_metadata(
                    root=root,
                    touched_repo_paths=touched_repo_paths,
                    command_was_verification=False,
                )
            )
        current_selection = _current_verify_selection()
        current_effective_verification_commands = _normalized_verify_commands(
            list(current_selection.commands) if current_selection is not None else []
        )
        shell_exit_code = result.get("exit_code") if isinstance(result, dict) else None
        evidence_v2 = _evidence_v2_enabled(cfg)
        shell_effective_cmd = str(result.get("effective_cmd") or result.get("cmd") or cmd)

        def _reexec_first_stage_exit(stage: str) -> int | None:
            # Bounded ground-truth fallback: when PIPESTATUS could not be observed
            # (e.g. bash was unavailable), re-run only a recognized test/execution
            # first stage unpiped, once, to learn its true exit code. Never re-run
            # an arbitrary side-effecting first stage.
            nonlocal command_seconds
            if not command_is_qualifying_execution_evidence(stage):
                return None
            rerun_started = perf_counter()
            try:
                rerun = _call_with_optional_kwargs(
                    _patchable("shell_run", shell_run),
                    required_kwargs={
                        "root": root,
                        "cmd": stage,
                        "cwd": effective_cwd,
                        "runner": shell_runner,
                    },
                    optional_kwargs={"timeout_s": shell_timeout_s},
                )
            except Exception:  # noqa: BLE001
                return None
            finally:
                # Still command time, not dispatch time, even when it fails.
                command_seconds += perf_counter() - rerun_started
            code = rerun.get("exit_code") if isinstance(rerun, dict) else None
            return code if isinstance(code, int) else None

        stage_status = result.get("pipeline_stage_status") if isinstance(result, dict) else None
        if evidence_v2 and stage_status is None:
            resolved_status = resolve_pipeline_stage_status(
                shell_effective_cmd,
                None,
                reexec=_reexec_first_stage_exit,
            )
            if resolved_status is not None:
                stage_status = resolved_status
                result["pipeline_stage_status"] = resolved_status
                result["pipeline_stage_status_source"] = "reexec"
        shell_evidence = classify_verification_evidence(
            shell_effective_cmd,
            known_verification_commands=current_effective_verification_commands,
            authoritative=(
                is_authoritative_verify_command_selection(current_selection)
                if current_selection is not None
                else bool(authoritative_verify_commands is not None)
            ),
            material_touched_paths=_verification_relevant_material_paths(
                result.get("material_touched_repo_paths", [])
                if isinstance(result.get("material_touched_repo_paths"), list)
                else [],
            ),
            exit_code=(shell_exit_code if isinstance(shell_exit_code, int) else None),
            output="\n".join(
                [
                    str(result.get("stdout") or "").strip(),
                    str(result.get("stderr") or "").strip(),
                ]
            ).strip(),
            root=root,
            stage_status=stage_status if isinstance(stage_status, list) else None,
            evidence_v2=evidence_v2,
        )
        result["verification_evidence_category"] = shell_evidence.category.value
        result["verification_evidence_reason"] = shell_evidence.reason
        result["verification_evidence_allowed"] = shell_evidence.allowed_to_satisfy_contract
        result["verification_evidence_supplemental_only"] = shell_evidence.supplemental_only
        result["evidence_verdict"] = shell_evidence.evidence_verdict
        result["dispatch_overhead_seconds"] = round(
            _observe_dispatch_overhead(
                dispatch_started=dispatch_started,
                command_seconds=command_seconds,
            ),
            6,
        )
        return result

    _append_builtin_tool("shell_run", run=_shell)

    def _require_terminal_manager() -> TerminalManager:
        if terminal_manager is None:
            raise AgentRuntimeError("Background shell tools are unavailable in this session.")
        return terminal_manager

    def _require_durable_service_manager() -> DurableServiceManager:
        if durable_service_manager is None:
            raise AgentRuntimeError("Durable service tools are unavailable in this session.")
        return durable_service_manager

    def _service_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
        readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
        return {
            "service_id": payload.get("service_id"),
            "ownership": payload.get("ownership") or ProcessOwnership.DURABLE_SERVICE.value,
            "status": payload.get("status"),
            "alive": bool(payload.get("alive")),
            "backend": payload.get("backend"),
            "readiness": {
                "type": readiness.get("type"),
                "status": readiness.get("status"),
                "host": readiness.get("host"),
                "port": readiness.get("port"),
                "path": readiness.get("path"),
            },
            "failure_category": payload.get("failure_category"),
            "log_paths": payload.get("log_paths"),
            "preview_url": payload.get("preview_url"),
            "startup_error": payload.get("startup_error"),
        }

    def _guard_service_readiness_spec(raw_readiness: Any) -> dict[str, Any] | None:
        if raw_readiness is None:
            return None
        if not isinstance(raw_readiness, dict):
            raise AgentRuntimeError("readiness must be an object when provided")
        readiness = dict(raw_readiness)
        if str(readiness.get("type") or "").strip().lower() == "command":
            command = str(readiness.get("command") or "").strip()
            if not command:
                raise AgentRuntimeError("readiness.command is required for command readiness")
            guard_shell(command, tool_name="shell_service_start")
        return readiness

    def _format_bg_snapshot(
        *,
        process_id: str,
        snapshot: ProcessOutputSnapshot,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        lines: list[dict[str, Any]] = []
        output_truncated_by_max_bytes = False
        remaining_bytes = max_bytes if max_bytes is not None else None
        for line in snapshot.lines:
            text = line.text
            if remaining_bytes is not None:
                encoded = text.encode("utf-8", errors="replace")
                if remaining_bytes <= 0:
                    output_truncated_by_max_bytes = True
                    break
                if len(encoded) > remaining_bytes:
                    text = encoded[:remaining_bytes].decode("utf-8", errors="replace")
                    output_truncated_by_max_bytes = True
                    remaining_bytes = 0
                else:
                    remaining_bytes -= len(encoded)
            lines.append({"seq": line.seq, "stream": line.stream, "text": text})
        payload = {
            "process_id": process_id,
            "lifetime": "session",
            "status": snapshot.status,
            "exit_code": snapshot.exit_code,
            "failure_reason": snapshot.failure_reason,
            "lines": lines,
            "next_seq": snapshot.next_seq,
            "dropped_lines": snapshot.dropped_lines,
            "runtime_s": round(snapshot.runtime_s, 3),
            "total_bytes": snapshot.total_bytes,
        }
        if output_truncated_by_max_bytes:
            payload["output_truncated_by_max_bytes"] = True
            payload["max_bytes"] = max_bytes
        return payload

    def _format_bg_summaries(manager: TerminalManager) -> list[dict[str, Any]]:
        return [
            {
                "process_id": summary.process_id,
                "cmd": summary.cmd,
                "cwd": str(summary.cwd),
                "status": summary.status,
                "exit_code": summary.exit_code,
                "runtime_s": round(summary.runtime_s, 3),
                "started_at_wall": summary.started_at_wall,
            }
            for summary in manager.list()
        ]

    def _unknown_bg_process_payload(
        *,
        manager: TerminalManager,
        process_id: str,
        operation: str,
        since: int | None = None,
    ) -> dict[str, Any]:
        known_processes = _format_bg_summaries(manager)
        payload: dict[str, Any] = {
            "status": "unknown_process_id",
            "unknown_process_id": True,
            "process_id": process_id,
            "requested_process_id": process_id,
            "operation": operation,
            "exit_code": None,
            "failure_reason": "No background process with that process_id is tracked in this session.",
            "lines": [],
            "next_seq": since if since is not None else 0,
            "dropped_lines": 0,
            "runtime_s": 0.0,
            "total_bytes": 0,
            "known_processes": known_processes,
            "known_process_ids": [process["process_id"] for process in known_processes],
            "recovery": {
                "recommended_tool": "shell_list",
                "suggested_arguments": {},
                "reason": (
                    "The supplied process_id is not tracked. Use shell_list or the process_id "
                    "returned by shell_background; do not use a tool_call_id as process_id."
                ),
            },
        }
        if since is not None:
            payload["since"] = since
        store.append(
            "bg_unknown_process",
            {
                "operation": operation,
                "process_id": process_id,
                "known_process_count": len(known_processes),
            },
        )
        return payload

    shell_empty_poll_counts: dict[tuple[str, int, int, str], int] = {}

    def _coerce_shell_since(raw_since: Any) -> int:
        try:
            since = int(raw_since) if raw_since is not None else 0
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError(f"Invalid since value: {raw_since!r}") from exc
        if since < 0:
            raise AgentRuntimeError("since must be non-negative")
        return since

    def _coerce_shell_wait_seconds(raw_wait: Any) -> float:
        try:
            wait_seconds = float(raw_wait) if raw_wait is not None else 5.0
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError(f"Invalid wait_seconds value: {raw_wait!r}") from exc
        if wait_seconds < 0:
            raise AgentRuntimeError("wait_seconds must be non-negative")
        return min(wait_seconds, 60.0)

    def _coerce_shell_max_bytes(raw_max_bytes: Any) -> int | None:
        if raw_max_bytes is None:
            return None
        try:
            max_bytes = int(raw_max_bytes)
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError(f"Invalid max_bytes value: {raw_max_bytes!r}") from exc
        if max_bytes <= 0:
            raise AgentRuntimeError("max_bytes must be positive")
        return max_bytes

    def _coerce_shell_wait_until(raw_until: Any) -> str:
        until = str(raw_until or "either").strip().lower()
        if until not in {"output_available", "process_exited", "either"}:
            raise AgentRuntimeError(
                "until must be one of output_available, process_exited, or either"
            )
        return until

    def _clamp_shell_wait_seconds(wait_seconds: float) -> tuple[float, dict[str, Any] | None]:
        if execution_deadline is None:
            return wait_seconds, None
        decision = execution_deadline.start_decision(
            DeadlineOperation.SHELL_TOOL,
            minimum_remaining_seconds=MINIMUM_TOOL_START_SECONDS,
            configured_timeout_seconds=wait_seconds,
            allow_during_finalization=True,
        ).telemetry_snapshot()
        if not bool(decision.get("allowed")):
            return 0.0, decision
        clamped = execution_deadline.clamp_timeout(
            wait_seconds,
            reserve_seconds=DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS,
        )
        if clamped is None:
            return 0.0, decision
        if execution_deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW:
            clamped = min(float(clamped), 1.0)
        return float(clamped), decision

    def _observe_dispatch_overhead(
        *,
        dispatch_started: float,
        command_seconds: float,
    ) -> float:
        """Record time spent in dispatch machinery, excluding the command itself.

        ``shell_tool`` and ``tool_dispatch`` both time the whole dispatch, so a
        build that legitimately takes twenty minutes is indistinguishable from a
        dispatch path that has become expensive. Subtracting the command's own
        runtime leaves the number that actually describes this code, reported as
        its own ``duration_observations`` category.
        """
        account = DispatchOverheadAccount.from_totals(
            perf_counter() - dispatch_started,
            command_seconds,
        )
        if execution_deadline is not None:
            execution_deadline.observe_duration(
                DISPATCH_OVERHEAD_OPERATION,
                account.overhead_seconds,
            )
        return account.overhead_seconds

    def _cancellation_probe(token: Any | None) -> Callable[[], bool] | None:
        """Read-only view of a cancellation token, or ``None`` when absent."""
        if token is None:
            return None
        return lambda: bool(getattr(token, "is_cancelled", False))

    def _wait_for_output_cancellably(
        manager: TerminalManager,
        *,
        process_id: str,
        since: int,
        timeout_s: float,
        until: str,
        cancellation_token: Any | None,
    ) -> tuple[ProcessOutputSnapshot, bool, bool]:
        """Wait for background output without outliving the run budget.

        The manager's wait is already completion-driven -- it returns the moment
        the process speaks or exits -- but it blocks on a condition variable that
        knows nothing about the budget. A ``shell_wait`` armed just before the
        deadline therefore ran to its full 60s while the run was already over,
        which is PR2's documented "a running tool cannot be preempted" gap in the
        one place the telemetry says it costs whole runs.

        Driving the same wait in slices and re-reading the token between them
        bounds cancellation latency to a single slice. Because each slice still
        returns early on completion, nothing about the non-cancelled path gets
        slower, and no busy-polling is introduced. When no token is supplied
        there is nothing to observe, so the wait is taken in one step exactly as
        before.

        Returns ``(snapshot, timed_out, cancelled)``.
        """
        latest_snapshot: ProcessOutputSnapshot | None = None

        def _wait_once(step: float) -> bool:
            nonlocal latest_snapshot
            snapshot, wait_timed_out = manager.wait_for_output(
                process_id,
                since=since,
                timeout_s=step,
                until=until,  # type: ignore[arg-type]
            )
            latest_snapshot = snapshot
            return not wait_timed_out

        is_cancelled = _cancellation_probe(cancellation_token)
        result = run_cancellable_wait(
            wait_once=_wait_once,
            total_seconds=timeout_s,
            is_cancelled=is_cancelled,
            slice_seconds=None if is_cancelled is not None else 0.0,
        )
        snapshot = (
            latest_snapshot
            if latest_snapshot is not None
            # Cancelled before the first block: report the current state rather
            # than an empty one, so nothing already emitted is dropped.
            else manager.read(process_id, since=since)
        )
        return snapshot, not result.completed, result.cancelled

    def _maybe_add_empty_poll_guidance(
        *,
        payload: dict[str, Any],
        process_id: str,
        since: int,
        snapshot: ProcessOutputSnapshot,
    ) -> None:
        if snapshot.lines or snapshot.status != "running":
            shell_empty_poll_counts.pop(
                (process_id, since, snapshot.next_seq, snapshot.status), None
            )
            return
        key = (process_id, since, snapshot.next_seq, snapshot.status)
        count = shell_empty_poll_counts.get(key, 0) + 1
        shell_empty_poll_counts[key] = count
        payload["empty_poll_count"] = count
        if count >= 2:
            payload["wait_guidance"] = {
                "recommended_tool": "shell_wait",
                "reason": "No new output or process status change was observed for repeated immediate polls.",
                "process_id": process_id,
                "since": since,
                "suggested_arguments": {
                    "process_id": process_id,
                    "since": since,
                    "until": "either",
                    "wait_seconds": 5,
                },
            }

    def _persist_background_start(
        *,
        cmd: str,
        cwd_path: Path,
        effective_cwd_relpath: str,
        probe_port: int | None,
        deadline_warning: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Route a persist=true background start to the durable-service manager.

        Persistence is deliberately not reimplemented here. The durable manager
        already spawns into its own session, redirects stdio to files under the
        session's service directory, and is excluded from every reaping path --
        so routing keeps one implementation and one lifecycle rather than a
        second, subtly different one.
        """

        manager = _require_durable_service_manager()
        readiness = readiness_spec_for_port(probe_port) if probe_port is not None else None
        try:
            started = manager.start(cmd=cmd, cwd=cwd_path, readiness=readiness)
        except ValueError as exc:
            raise AgentRuntimeError(f"Invalid durable service request: {exc}") from exc
        except (ConfigError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise AgentRuntimeError(f"Failed to start durable service: {exc}") from exc
        payload = dict(started.payload)
        payload["lifetime"] = "durable"
        payload["persist"] = True
        record = PersistentServiceRecord(
            service_id=started.service_id,
            command=cmd,
            pid=int(payload.get("pid") or 0),
            probe_port=probe_port,
        )
        # A start that silently produced nothing is the failure this PR exists
        # for, so say now whether the process is up and the port is answering.
        payload.update(check_service(record).as_payload())
        if persistent_service_registry is not None:
            persistent_service_registry.register(record)
        store.append(
            "service_start",
            {
                **_service_event_payload(payload),
                "cwd": effective_cwd_relpath,
                "persist": True,
                "probe_port": probe_port,
            },
        )
        if deadline_warning is not None:
            payload.update(deadline_warning)
        return payload

    def _shell_background(args: dict[str, Any]) -> dict[str, Any]:
        deadline_decision = _deadline_start_decision(
            DeadlineOperation.SHELL_BACKGROUND,
            minimum_remaining_seconds=MINIMUM_TOOL_START_SECONDS,
        )
        deadline_warning = None
        if deadline_decision is not None and not bool(deadline_decision.get("allowed")):
            deadline_warning = _deadline_warning_fields(
                "Deadline policy would normally block background work; start proceeded because "
                "this is advisory and not safety.",
                start_decision=deadline_decision,
            )
        manager = _require_terminal_manager()
        cmd = str(args.get("cmd", ""))
        effective_cwd_relpath = _resolve_workspace_relative_path(
            tool_name="shell_background",
            raw_path=args.get("cwd"),
            raw_base=args.get("cwd_base"),
            field_name="cwd",
            base_field_name="cwd_base",
            allow_empty=True,
        )
        guard_shell(cmd, tool_name="shell_background")
        cwd_path = root if not effective_cwd_relpath else (root / effective_cwd_relpath).resolve()
        try:
            probe_port = resolve_probe_port(requested=args.get("probe_port"), cmd=cmd)
        except ValueError as exc:
            raise AgentRuntimeError(str(exc)) from exc
        if bool(args.get("persist")):
            return _persist_background_start(
                cmd=cmd,
                cwd_path=cwd_path,
                effective_cwd_relpath=effective_cwd_relpath,
                probe_port=probe_port,
                deadline_warning=deadline_warning,
            )
        store.append("bg_start", {"cmd": cmd, "cwd": effective_cwd_relpath})
        started = perf_counter()
        snapshot: ProcessOutputSnapshot | None = None
        try:
            try:
                process_id = manager.start(
                    cmd=cmd,
                    cwd=cwd_path,
                    root=root,
                )
            except TerminalLimitError as exc:
                raise AgentRuntimeError(str(exc)) from exc
            except ValueError as exc:
                raise AgentRuntimeError(f"Invalid background process request: {exc}") from exc
            except (ConfigError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                raise AgentRuntimeError(f"Failed to start background process: {exc}") from exc
            snapshot = manager.read(process_id)
            payload = _format_bg_snapshot(process_id=process_id, snapshot=snapshot)
            if deadline_warning is not None:
                payload.update(deadline_warning)
            return payload
        finally:
            if is_full_access_mode:
                duration_ms = int((perf_counter() - started) * 1000)
                exit_code = snapshot.exit_code if snapshot is not None else None
                store.append(
                    "fullaccess_shell",
                    {
                        "event": "fullaccess_shell",
                        "ts": _fullaccess_shell_audit_ts(),
                        "command": cmd,
                        "cwd": str(cwd_path),
                        "exit_code": int(exit_code if exit_code is not None else -1),
                        "duration_ms": duration_ms,
                        "mode": "fullaccess",
                    },
                )

    def _shell_output(args: dict[str, Any]) -> dict[str, Any]:
        manager = _require_terminal_manager()
        guard_terminal_op("shell_output")
        process_id = str(args.get("process_id", "")).strip()
        if not process_id:
            raise AgentRuntimeError("Missing required argument: process_id")
        since = _coerce_shell_since(args.get("since"))
        try:
            snapshot = manager.read(process_id, since=since)
        except KeyError:
            return _unknown_bg_process_payload(
                manager=manager,
                process_id=process_id,
                operation="shell_output",
                since=since,
            )
        payload = _format_bg_snapshot(process_id=process_id, snapshot=snapshot)
        _maybe_add_empty_poll_guidance(
            payload=payload,
            process_id=process_id,
            since=since,
            snapshot=snapshot,
        )
        return payload

    def _shell_wait(args: dict[str, Any]) -> dict[str, Any]:
        dispatch_started = perf_counter()
        manager = _require_terminal_manager()
        guard_terminal_op("shell_wait")
        process_id = str(args.get("process_id", "")).strip()
        if not process_id:
            raise AgentRuntimeError("Missing required argument: process_id")
        since = _coerce_shell_since(args.get("since"))
        wait_seconds = _coerce_shell_wait_seconds(args.get("wait_seconds"))
        until = _coerce_shell_wait_until(args.get("until"))
        max_bytes = _coerce_shell_max_bytes(args.get("max_bytes"))
        clamped_wait_seconds, deadline_decision = _clamp_shell_wait_seconds(wait_seconds)
        started = perf_counter()
        wait_cancelled = False
        try:
            snapshot, timed_out, wait_cancelled = _wait_for_output_cancellably(
                manager,
                process_id=process_id,
                since=since,
                timeout_s=clamped_wait_seconds,
                until=until,
                cancellation_token=args.get(_SHELL_CANCELLATION_TOKEN_ARG),
            )
        except KeyError:
            payload = _unknown_bg_process_payload(
                manager=manager,
                process_id=process_id,
                operation="shell_wait",
                since=since,
            )
            payload.update(
                {
                    "waited": False,
                    "timed_out": False,
                    "wait_seconds_requested": wait_seconds,
                    "wait_seconds_effective": 0.0,
                    "until": until,
                    "elapsed_ms": int((perf_counter() - started) * 1000),
                }
            )
            if deadline_decision is not None:
                payload["deadline_start_decision"] = deadline_decision
                payload["deadline_clamped"] = clamped_wait_seconds < wait_seconds
            _observe_dispatch_overhead(
                dispatch_started=dispatch_started,
                command_seconds=perf_counter() - started,
            )
            return payload
        waited_seconds = perf_counter() - started
        elapsed_ms = int(waited_seconds * 1000)
        payload = _format_bg_snapshot(
            process_id=process_id,
            snapshot=snapshot,
            max_bytes=max_bytes,
        )
        payload.update(
            {
                "waited": True,
                "timed_out": timed_out,
                "wait_seconds_requested": wait_seconds,
                "wait_seconds_effective": clamped_wait_seconds,
                "until": until,
                "elapsed_ms": elapsed_ms,
            }
        )
        if wait_cancelled:
            # The watchdog fired mid-wait. Reported rather than raised: the step
            # loop's existing cancellation checkpoint stops the run on its next
            # iteration, and returning normally keeps the output collected so
            # far instead of discarding it with an exception.
            payload["wait_interrupted_by_budget"] = True
        if deadline_decision is not None:
            payload["deadline_start_decision"] = deadline_decision
            payload["deadline_clamped"] = clamped_wait_seconds < wait_seconds
        payload["dispatch_overhead_seconds"] = round(
            _observe_dispatch_overhead(
                dispatch_started=dispatch_started,
                command_seconds=waited_seconds,
            ),
            6,
        )
        return payload

    def _shell_kill(args: dict[str, Any]) -> dict[str, Any]:
        manager = _require_terminal_manager()
        guard_terminal_op("shell_kill")
        process_id = str(args.get("process_id", "")).strip()
        if not process_id:
            raise AgentRuntimeError("Missing required argument: process_id")
        try:
            snapshot = manager.kill(process_id)
        except KeyError as exc:
            raise AgentRuntimeError(f"Unknown background process_id: {process_id}") from exc
        store.append(
            "bg_kill",
            {
                "process_id": process_id,
                "status": snapshot.status,
                "exit_code": snapshot.exit_code,
            },
        )
        return _format_bg_snapshot(process_id=process_id, snapshot=snapshot)

    def _shell_list(_args: dict[str, Any]) -> dict[str, Any]:
        manager = _require_terminal_manager()
        guard_terminal_op("shell_list")
        return {"processes": _format_bg_summaries(manager)}

    def _shell_service_start(args: dict[str, Any]) -> dict[str, Any]:
        deadline_decision = _deadline_start_decision(
            DeadlineOperation.SHELL_BACKGROUND,
            minimum_remaining_seconds=MINIMUM_TOOL_START_SECONDS,
        )
        deadline_warning = None
        if deadline_decision is not None and not bool(deadline_decision.get("allowed")):
            deadline_warning = _deadline_warning_fields(
                "Deadline policy would normally block service work; start proceeded because "
                "this is advisory and not safety.",
                start_decision=deadline_decision,
            )
        manager = _require_durable_service_manager()
        cmd = str(args.get("cmd", ""))
        guard_shell(cmd, tool_name="shell_service_start")
        readiness = _guard_service_readiness_spec(args.get("readiness"))
        effective_cwd_relpath = _resolve_workspace_relative_path(
            tool_name="shell_service_start",
            raw_path=args.get("cwd"),
            raw_base=args.get("cwd_base"),
            field_name="cwd",
            base_field_name="cwd_base",
            allow_empty=True,
        )
        cwd_path = root if not effective_cwd_relpath else (root / effective_cwd_relpath).resolve()
        try:
            started = manager.start(cmd=cmd, cwd=cwd_path, readiness=readiness)
        except ValueError as exc:
            raise AgentRuntimeError(f"Invalid durable service request: {exc}") from exc
        except (ConfigError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise AgentRuntimeError(f"Failed to start durable service: {exc}") from exc
        payload = dict(started.payload)
        payload["lifetime"] = "durable"
        if deadline_warning is not None:
            payload.update(deadline_warning)
        store.append(
            "service_start",
            {
                **_service_event_payload(payload),
                "cwd": effective_cwd_relpath,
            },
        )
        return payload

    def _workspace_preview_start(args: dict[str, Any]) -> dict[str, Any]:
        deadline_decision = _deadline_start_decision(
            DeadlineOperation.SHELL_BACKGROUND,
            minimum_remaining_seconds=MINIMUM_TOOL_START_SECONDS,
        )
        deadline_warning = None
        if deadline_decision is not None and not bool(deadline_decision.get("allowed")):
            deadline_warning = _deadline_warning_fields(
                "Deadline policy would normally block preview work; start proceeded because "
                "this is advisory and not safety.",
                start_decision=deadline_decision,
            )
        manager = _require_durable_service_manager()
        guard_terminal_op("workspace_preview_start")
        requested_access = str(args.get("access") or "auto").strip().lower()
        try:
            effective_access = manager.resolve_preview_access(requested_access)
        except ValueError as exc:
            raise AgentRuntimeError(f"Invalid workspace preview request: {exc}") from exc
        if effective_access == "lan" and not yes and not is_full_access_mode:
            if non_interactive and not host_managed_approvals:
                raise AgentRuntimeError(
                    "LAN preview exposure requires interactive approval. Use local access or "
                    "re-run in an approval-capable session."
                )
            decision = surface.request_approval(
                ApprovalRequest(
                    kind="workspace_preview_lan",
                    reason=(
                        "LAN preview access exposes an authenticated workspace server to other "
                        "devices on the current network"
                    ),
                    preview="Start a temporary authenticated LAN workspace preview",
                )
            )
            if not decision.allow:
                raise ApprovalDeclinedError("workspace_preview_lan")
        raw_port = args.get("port")
        if raw_port is None or raw_port == "":
            port = None
        else:
            if isinstance(raw_port, bool):
                raise AgentRuntimeError("Preview port must be an integer")
            try:
                port = int(raw_port)
            except (TypeError, ValueError) as exc:
                raise AgentRuntimeError("Preview port must be an integer") from exc
        effective_cwd_relpath = _resolve_workspace_relative_path(
            tool_name="workspace_preview_start",
            raw_path=args.get("cwd"),
            raw_base=args.get("cwd_base"),
            field_name="cwd",
            base_field_name="cwd_base",
            allow_empty=True,
        )
        cwd_path = root if not effective_cwd_relpath else (root / effective_cwd_relpath).resolve()
        try:
            started = manager.start_preview(
                cwd=cwd_path,
                access=requested_access,
                port=port,
            )
        except ValueError as exc:
            raise AgentRuntimeError(f"Invalid workspace preview request: {exc}") from exc
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise AgentRuntimeError(f"Failed to start workspace preview: {exc}") from exc
        payload = dict(started.payload)
        payload["lifetime"] = "durable"
        if deadline_warning is not None:
            payload.update(deadline_warning)
        store.append(
            "service_start",
            {
                **_service_event_payload(payload),
                "cwd": effective_cwd_relpath,
                "service_kind": "workspace_preview",
            },
        )
        return payload

    def _shell_service_status(args: dict[str, Any]) -> dict[str, Any]:
        manager = _require_durable_service_manager()
        guard_terminal_op("shell_service_status")
        service_id = str(args.get("service_id", "")).strip()
        if not service_id:
            raise AgentRuntimeError("Missing required argument: service_id")
        try:
            payload = manager.status(service_id)
        except ValueError as exc:
            raise AgentRuntimeError(f"Invalid durable service_id: {exc}") from exc
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise AgentRuntimeError(f"Failed to inspect durable service: {exc}") from exc
        store.append("service_status", _service_event_payload(payload))
        return payload

    def _shell_service_stop(args: dict[str, Any]) -> dict[str, Any]:
        manager = _require_durable_service_manager()
        guard_terminal_op("shell_service_stop")
        service_id = str(args.get("service_id", "")).strip()
        if not service_id:
            raise AgentRuntimeError("Missing required argument: service_id")
        try:
            payload = manager.stop(service_id)
        except ValueError as exc:
            raise AgentRuntimeError(f"Invalid durable service_id: {exc}") from exc
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise AgentRuntimeError(f"Failed to stop durable service: {exc}") from exc
        store.append("service_stop", _service_event_payload(payload))
        return payload

    _append_builtin_tool("shell_background", run=_shell_background)
    _append_builtin_tool("shell_output", run=_shell_output)
    _append_builtin_tool("shell_wait", run=_shell_wait)
    _append_builtin_tool("shell_kill", run=_shell_kill)
    _append_builtin_tool("shell_list", run=_shell_list)
    _append_builtin_tool("shell_service_start", run=_shell_service_start)
    _append_builtin_tool("workspace_preview_start", run=_workspace_preview_start)
    _append_builtin_tool("shell_service_status", run=_shell_service_status)
    _append_builtin_tool("shell_service_stop", run=_shell_service_stop)

    def _session_set_workdir(args: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(args.get("path", "")).strip()
        if not raw_path:
            raise SessionWorkdirError("Missing required argument: path")
        if not callable(set_active_workdir_callback):
            raise SessionWorkdirError("session_set_workdir is unavailable in this session.")
        return set_active_workdir_callback(raw_path, "tool")

    _append_builtin_tool("session_set_workdir", run=_session_set_workdir)

    def _host_action(tool_name: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if host_action_handler is None or action not in effective_host_actions:
            raise AgentRuntimeError(
                f"{tool_name} is unavailable because the IDE host did not advertise this capability."
            )
        try:
            normalized = normalize_host_action_arguments(action, args)
            if action in {"tasks.run", "debug.start"}:
                if persona_write_scope_active:
                    raise AgentRuntimeError(
                        f"Blocked while persona write scope is active: {tool_name}. "
                        "Opaque IDE executions cannot be constrained to persona paths."
                    )
                if mode == "readonly":
                    raise AgentRuntimeError(f"Blocked in readonly mode: {tool_name}")
                elif not host_managed_approvals:
                    raise AgentRuntimeError(
                        f"Explicit one-time IDE approval is required for {tool_name}."
                    )
                else:
                    identifier_field = "task_id" if action == "tasks.run" else "configuration_id"
                    opaque_id = normalized[identifier_field]
                    decision = surface.request_approval(
                        ApprovalRequest(
                            kind=tool_name,
                            reason=(
                                "opaque IDE task and debug configurations may execute arbitrary "
                                "workspace commands"
                            ),
                            preview=(
                                f"IDE host execution: {action}\n"
                                f"{identifier_field}: {opaque_id}\n"
                                f"workspace: {root}"
                            ),
                            metadata={
                                "mandatory_explicit_approval": True,
                                "allow_for_session_disabled": True,
                                "host_action": action,
                                "opaque_id": opaque_id,
                                "workspace_root": str(root),
                            },
                            # Opaque task/configuration ids can resolve to changed commands later.
                            # A cached or broad session grant must never authorize execution.
                            allow_for_session_scope=None,
                        )
                    )
                    if not decision.allow:
                        raise ApprovalDeclinedError(tool_name)
                    if decision.allow_for_session:
                        raise AgentRuntimeError(
                            f"Session approval cannot authorize opaque IDE execution: {tool_name}."
                        )
            return host_action_handler(action, normalized)
        except HostActionError as exc:
            raise AgentRuntimeError(exc.message, result_payload=exc.to_result_payload()) from exc

    host_tool_actions = {tool_name: action for action, tool_name in HOST_ACTION_TOOL_NAMES.items()}
    for tool_name, action in host_tool_actions.items():
        if action not in effective_host_actions or host_action_handler is None:
            continue

        def _run_host_action(
            args: dict[str, Any],
            *,
            _tool_name: str = tool_name,
            _action: str = action,
        ) -> dict[str, Any]:
            if _action in {"tasks.terminate", "debug.stop"}:
                guard_terminal_op(_tool_name)
            return _host_action(_tool_name, _action, args)

        _append_builtin_tool(tool_name, run=_run_host_action)

    _append_builtin_tool(
        "git_status",
        run=lambda _args: git_status(root=root),
    )

    _append_builtin_tool(
        "git_diff",
        run=lambda _args: git_diff(root=root),
    )

    if git_backed_workspace:
        _append_builtin_tool(
            "git_history",
            run=lambda args: git_history(
                root=root,
                mode=str(args.get("mode", "")),
                path=str(args["path"]) if args.get("path") is not None else None,
                limit=int(args["limit"]) if args.get("limit") is not None else 10,
                ref=str(args["ref"]) if args.get("ref") is not None else None,
                grep=str(args["grep"]) if args.get("grep") is not None else None,
                author=str(args["author"]) if args.get("author") is not None else None,
                commit=str(args["commit"]) if args.get("commit") is not None else None,
                start_line=(
                    int(args["start_line"]) if args.get("start_line") is not None else None
                ),
                end_line=(int(args["end_line"]) if args.get("end_line") is not None else None),
            ),
        )

    def _git_apply(args: dict[str, Any]) -> dict[str, Any]:
        patch = str(args.get("patch", ""))
        patch_paths = sorted(set(iter_patch_paths(patch)))
        for p in patch_paths:
            _guard_write_path(p)
        preconditions = [capture_file_precondition(root=root, path=p) for p in patch_paths]
        sensitive = guard_sensitive_files("git_apply_patch", files=patch_paths)
        preview = patch[:20000]
        if sensitive:
            store.append(
                "sensitive_change_preview",
                {
                    "paths": patch_paths,
                    "operation": "git_apply_patch",
                    "content_redacted": True,
                },
            )
        else:
            store.append("diff_preview", {"patch": preview})
            surface.on_patch_generated(
                PatchEvent(
                    files=patch_paths,
                    diff=patch,
                    summary=f"{len(patch_paths)} file(s) changed via git_apply_patch",
                )
            )
            guard_write("git_apply_patch", preview or "(empty patch)", files=patch_paths)
        try:
            for precondition in preconditions:
                assert_file_precondition(root=root, precondition=precondition)
            result = git_apply_patch(root=root, patch=patch)
        except StaleFileError as error:
            return _stale_file_result(error)
        return _mark_sensitive_result(result, sensitive)

    _append_builtin_tool("git_apply_patch", run=_git_apply)

    if subagents_enabled and subagent_depth == 0:
        callable_subagent_names = routable_subagent_names(
            registry=subagent_registry,
            cfg=cfg,
            available_tool_names={tool.name for tool in tools},
        )
        subagent_parameters = copied_tool_parameters("subagent_run")
        properties = subagent_parameters.get("properties")
        if not isinstance(properties, dict):
            raise AgentRuntimeError("subagent_run parameters must define properties")
        subagent_name_schema = properties.get("name")
        if not isinstance(subagent_name_schema, dict):
            raise AgentRuntimeError("subagent_run parameters must define a name property")
        if callable_subagent_names:
            subagent_name_schema["enum"] = callable_subagent_names

        child_run_registry = ChildRunRegistry()
        workspace_provider = (
            SubagentWorkspaceProvider(root=root, store=store)
            if (
                (cfg is None or cfg.subagent_orchestration.workspace_isolation_enabled)
                and isinstance(store, SessionStore)
            )
            else None
        )
        subagent_launcher = SubagentLauncher(
            root=root,
            surface=surface,
            store=store,
            mode=mode,
            yes=yes,
            cfg=cfg,
            api_key=api_key,
            max_steps=max_steps,
            no_log=no_log,
            usage_role=usage_role,
            usage_summary=usage_summary,
            deny_write_prefixes=deny_write_prefixes,
            allow_write_globs=allow_write_globs,
            persona_allow_write_globs=persona_allow_write_globs,
            non_interactive=non_interactive,
            verification_enabled=verification_enabled,
            authoritative_verification_commands=authoritative_verify_commands,
            subagents_enabled=subagents_enabled,
            subagent_depth=subagent_depth,
            subagent_registry=subagent_registry,
            session_log_dir_override=session_log_dir_override,
            step_budget_runtime=step_budget_runtime,
            get_active_workdir_relpath=get_active_workdir_relpath,
            create_session_factory=create_session_factory,
            prompt_cache_parent_session_id=prompt_cache_parent_session_id,
            execution_deadline=execution_deadline,
            crash_diagnostic_log_path=crash_diagnostic_log_path,
            crash_diagnostics=crash_diagnostics,
            tools=tools,
            command_mutation_metadata=_command_mutation_metadata,
            workspace_provider=workspace_provider,
            child_run_registry=child_run_registry,
            helpers_enabled_for_children=(
                resolved_runtime_kind
                not in {
                    RuntimeKind.FORGE_EXEC,
                    RuntimeKind.SWARM_WORKER,
                }
            ),
            managed_browser_service=managed_browser_service,
            managed_browser_owner_id=managed_browser_owner_id,
            managed_browser_cancel_check=managed_browser_cancel_check,
        )
        child_scheduler = ChildScheduler(
            launcher=subagent_launcher,
            max_background_children=(
                cfg.subagent_orchestration.max_background_children if cfg is not None else 3
            ),
            parent_steer_inbox=parent_steer_inbox,
        )
        if child_scheduler_sink is not None:
            child_scheduler_sink(child_scheduler)
        _append_builtin_tool(
            "subagent_run",
            parameters=subagent_parameters,
            run=subagent_launcher.run,
        )
        background_tools_enabled = resolved_runtime_kind not in {
            RuntimeKind.FORGE_EXEC,
            RuntimeKind.SWARM_WORKER,
        }
        if background_tools_enabled:
            spawn_parameters = copied_tool_parameters("subagent_spawn")
            spawn_properties = spawn_parameters.get("properties")
            if not isinstance(spawn_properties, dict):
                raise AgentRuntimeError("subagent_spawn parameters must define properties")
            spawn_name_schema = spawn_properties.get("name")
            if not isinstance(spawn_name_schema, dict):
                raise AgentRuntimeError("subagent_spawn parameters must define a name property")
            if callable_subagent_names:
                spawn_name_schema["enum"] = callable_subagent_names

            def _subagent_spawn(args: dict[str, Any]) -> dict[str, Any]:
                parent_token = args.get(_SUBAGENT_CANCELLATION_TOKEN_ARG)
                public_args = {key: value for key, value in args.items() if isinstance(key, str)}
                return child_scheduler.spawn(
                    public_args,
                    parent_cancellation_token=parent_token,
                )

            def _subagent_status(args: dict[str, Any]) -> dict[str, Any]:
                raw_run_id = str(args.get("run_id") or "").strip()
                return child_scheduler.status(run_id=raw_run_id or None)

            def _subagent_send(args: dict[str, Any]) -> dict[str, Any]:
                return child_scheduler.send(
                    run_id=str(args.get("run_id") or "").strip(),
                    message=str(args.get("message") or ""),
                )

            def _subagent_resume(args: dict[str, Any]) -> dict[str, Any]:
                return child_scheduler.resume(
                    args,
                    parent_cancellation_token=args.get(_SUBAGENT_CANCELLATION_TOKEN_ARG),
                )

            def _subagent_wait(args: dict[str, Any]) -> dict[str, Any]:
                raw_run_id = str(args.get("run_id") or "all").strip() or "all"
                raw_timeout = args.get("timeout_s")
                timeout_s = float(raw_timeout) if raw_timeout is not None else None
                return child_scheduler.collect(
                    run_id=raw_run_id,
                    timeout_s=timeout_s,
                    cancellation_token=args.get(_SUBAGENT_CANCELLATION_TOKEN_ARG),
                )

            def _subagent_cancel(args: dict[str, Any]) -> dict[str, Any]:
                raw_run_id = str(args.get("run_id") or "all").strip() or "all"
                return child_scheduler.cancel(run_id=raw_run_id, wait_for_running=False)

            _append_builtin_tool(
                "subagent_spawn",
                parameters=spawn_parameters,
                run=_subagent_spawn,
            )
            _append_builtin_tool("subagent_send", run=_subagent_send)
            _append_builtin_tool("subagent_resume", run=_subagent_resume)
            _append_builtin_tool("subagent_status", run=_subagent_status)
            _append_builtin_tool("subagent_wait", run=_subagent_wait)
            _append_builtin_tool("subagent_cancel", run=_subagent_cancel)

        workspace_actions_enabled = bool(
            workspace_provider is not None
            and (cfg is None or cfg.subagent_orchestration.workspace_isolation_enabled)
        )
        if workspace_actions_enabled:

            def _subagent_apply(args: dict[str, Any]) -> dict[str, Any]:
                run_id = str(args.get("run_id") or "").strip()
                workspace_record = workspace_provider.get(run_id)
                if workspace_record is not None and workspace_record.no_changes:
                    return workspace_provider.apply(run_id)
                preflight = child_scheduler.candidate_apply_preflight(
                    run_id=run_id,
                    acknowledge_incomplete=args.get("acknowledge_incomplete") is True,
                )
                if not bool(preflight.get("allowed")):
                    return {key: value for key, value in preflight.items() if key != "allowed"}
                result = workspace_provider.apply(run_id)
                if preflight.get("incomplete_acknowledged"):
                    result.update(
                        {key: value for key, value in preflight.items() if key != "allowed"}
                    )
                return result

            def _subagent_discard(args: dict[str, Any]) -> dict[str, Any]:
                return workspace_provider.release(
                    str(args.get("run_id") or "").strip(),
                    action="discarded",
                )

            _append_builtin_tool("subagent_apply", run=_subagent_apply)
            _append_builtin_tool("subagent_discard", run=_subagent_discard)

    elif (
        helper_subagents_enabled
        and subagent_depth == 1
        and (cfg is None or cfg.subagent_orchestration.helpers_enabled)
    ):
        available_names = {tool.name for tool in tools}
        helper_names = helper_subagent_names(
            registry=subagent_registry,
            cfg=cfg,
            available_tool_names=available_names,
        )
        if helper_names and EDIT_CAPABLE_SUBAGENT_TOOL_NAMES.intersection(available_names):
            helper_parameters = copied_tool_parameters("subagent_run")
            helper_properties = helper_parameters.get("properties")
            if not isinstance(helper_properties, dict):
                raise AgentRuntimeError("subagent_run parameters must define properties")
            helper_parameters["properties"] = {
                name: schema
                for name, schema in helper_properties.items()
                if name in {"name", "task", "max_steps"}
            }
            helper_name_schema = helper_parameters["properties"].get("name")
            if not isinstance(helper_name_schema, dict):
                raise AgentRuntimeError("subagent_run parameters must define a name property")
            helper_name_schema["enum"] = helper_names
            helper_launcher = SubagentLauncher(
                root=root,
                surface=surface,
                store=store,
                mode=mode,
                yes=yes,
                cfg=cfg,
                api_key=api_key,
                max_steps=max_steps,
                no_log=no_log,
                usage_role=usage_role,
                usage_summary=usage_summary,
                deny_write_prefixes=deny_write_prefixes,
                allow_write_globs=allow_write_globs,
                persona_allow_write_globs=persona_allow_write_globs,
                non_interactive=non_interactive,
                verification_enabled=verification_enabled,
                authoritative_verification_commands=authoritative_verify_commands,
                subagents_enabled=True,
                subagent_depth=subagent_depth,
                subagent_registry=subagent_registry,
                session_log_dir_override=session_log_dir_override,
                step_budget_runtime=step_budget_runtime,
                get_active_workdir_relpath=get_active_workdir_relpath,
                create_session_factory=create_session_factory,
                prompt_cache_parent_session_id=prompt_cache_parent_session_id,
                execution_deadline=execution_deadline,
                crash_diagnostic_log_path=crash_diagnostic_log_path,
                crash_diagnostics=crash_diagnostics,
                tools=tools,
                command_mutation_metadata=_command_mutation_metadata,
                helper_only=True,
                helper_allowed_names=tuple(helper_names),
            )
            helper_description = (
                "Run one bounded non-editing helper in this workspace and return its "
                "advisory report. Helpers cannot delegate further."
            )
            tools.append(
                ToolDef(
                    name="subagent_run",
                    description=helper_description,
                    parameters=helper_parameters,
                    run=helper_launcher.run,
                    metadata={
                        "tool_type": "builtin",
                        "compact_parameters_for_model": True,
                        "model_description": helper_description,
                    },
                )
            )

    for custom_tool_spec in sorted(
        custom_tool_session_state.exposed_tools_by_name.values(),
        key=lambda spec: spec.name.casefold(),
    ):
        tools.append(
            ToolDef(
                name=custom_tool_spec.name,
                description=custom_tool_spec.description,
                parameters=copy.deepcopy(custom_tool_spec.input_schema),
                run=lambda args, spec=custom_tool_spec: _run_custom_tool(spec, args),
                metadata={
                    "tool_type": "custom_tool",
                    "compact_parameters_for_model": True,
                    "model_description_max_chars": 1200,
                    "custom_tool": custom_tool_spec.metadata(include_output_schema=True),
                },
            )
        )

    if mcp_manager is not None and _mcp_tool_exposed_in_mode(
        mode=mode,
        write_scope_restricted=persona_write_scope_active,
    ):
        for binding in mcp_manager.tool_bindings:
            bound_binding = binding.bind_session_mode(mode)
            tools.append(
                ToolDef(
                    name=bound_binding.tool_alias,
                    description=bound_binding.description,
                    parameters=bound_binding.parameters,
                    run=bound_binding.run,
                    metadata={
                        "tool_type": "mcp",
                        "compact_parameters_for_model": True,
                        "model_description_max_chars": 1000,
                    },
                )
            )
    active_tools = {t.name: t for t in tools}
    if tool_dispatch_guard is not None:

        def _resolve_guard_rel_path(
            *,
            raw_path: Any,
            raw_base: Any = None,
            field_name: str,
            base_field_name: str,
        ) -> str:
            return _resolve_workspace_relative_path(
                tool_name="tool_dispatch_guard",
                raw_path=raw_path,
                raw_base=raw_base,
                field_name=field_name,
                base_field_name=base_field_name,
            )

        def _wrap_with_dispatch_guard(tool: ToolDef) -> ToolDef:
            original_run = tool.run

            def guarded_run(
                arguments: dict[str, Any],
                *,
                _original_run: Callable[[dict[str, Any]], dict[str, Any]] = original_run,
                _tool_name: str = tool.name,
            ) -> dict[str, Any]:
                tool_dispatch_guard.check_tool_call(
                    _tool_name,
                    arguments if isinstance(arguments, dict) else {},
                    resolve_rel_path=_resolve_guard_rel_path,
                )
                return _original_run(arguments)

            return replace(tool, run=guarded_run)

        active_tools = {
            name: _wrap_with_dispatch_guard(tool) for name, tool in active_tools.items()
        }
    for metadata in iter_builtin_tool_metadata():
        register_tool_availability(metadata.name, optional=metadata.optional)
        if metadata.name in active_tools:
            mark_available(metadata.name)
        elif metadata.optional:
            if metadata.name == "image_generate" and (
                cfg is None or not cfg.image_generation.enabled
            ):
                reason = "image_generation.enabled is false"
            elif metadata.name == "image_generate":
                reason = f"image generation is not exposed in mode={mode}"
            else:
                reason = metadata.optional_unavailable_reason or (
                    "not registered in active tool registry "
                    f"for mode={mode} runtime_kind={resolved_runtime_kind.value}"
                )
            mark_unavailable(metadata.name, reason)
    return active_tools
