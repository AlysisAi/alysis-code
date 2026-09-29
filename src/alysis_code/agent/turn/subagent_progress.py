from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from dataclasses import dataclass, replace
from typing import Any


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_lower(value: Any) -> str:
    return _clean_text(value).casefold()


def _sorted_strings(*values: Any) -> tuple[str, ...]:
    items: set[str] = set()
    for value in values:
        if not isinstance(value, (list, tuple, set, frozenset)):
            continue
        for raw_item in value:
            item = _clean_text(raw_item)
            if item:
                items.add(item)
    return tuple(sorted(items))


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _normalized_launch_task(value: Any) -> str:
    """Normalize presentation-only whitespace in a delegated task brief.

    Providers may reflow the same natural-language brief between turns.  The
    launch identity must survive that transport variation without interpreting
    the brief's language or treating case-sensitive tokens as equivalent.
    """

    return " ".join(str(value or "").split())


def _workspace_payload(
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    raw_workspace = result.get("workspace")
    workspace = raw_workspace if isinstance(raw_workspace, dict) else {}
    patch_summary_raw = result.get("patch_summary")
    patch_summary = patch_summary_raw if isinstance(patch_summary_raw, dict) else {}

    view = _clean_lower(
        workspace.get("view")
        or result.get("workspace_view")
        or arguments.get("workspace_view")
        or "shared"
    )
    state = _clean_lower(workspace.get("state") or result.get("workspace_state"))
    if (
        not state
        and view == "isolated"
        and (patch_summary or isinstance(workspace.get("no_changes"), bool))
    ):
        state = "captured"
    payload: dict[str, Any] = {
        "view": view,
        "base_commit": _clean_text(workspace.get("base_commit") or result.get("base_commit")),
        "state": state,
        "parent_dirty_paths": _sorted_strings(
            workspace.get("parent_dirty_paths"),
            result.get("parent_dirty_paths"),
        ),
    }
    no_changes = workspace.get("no_changes", result.get("no_changes"))
    if isinstance(no_changes, bool):
        payload["no_changes"] = no_changes
    return payload


def _patch_sha256(result: dict[str, Any]) -> str:
    patch_summary_raw = result.get("patch_summary")
    patch_summary = patch_summary_raw if isinstance(patch_summary_raw, dict) else {}
    patch_raw = result.get("patch")
    patch = patch_raw if isinstance(patch_raw, dict) else {}
    material_identity_raw = result.get("material_identity")
    material_identity = material_identity_raw if isinstance(material_identity_raw, dict) else {}
    declared = _clean_lower(
        patch_summary.get("sha256")
        or result.get("patch_sha256")
        or patch.get("sha256")
        or material_identity.get("patch_sha256")
    )
    if declared:
        return declared
    if isinstance(patch_raw, str) and patch_raw:
        return hashlib.sha256(patch_raw.encode("utf-8", errors="replace")).hexdigest()
    return ""


def _stable_subagent_outcome(
    *,
    arguments: dict[str, Any],
    result: dict[str, Any],
    tool_status: str,
) -> dict[str, Any]:
    """Project a subagent result onto stable, objective outcome fields.

    The projection intentionally excludes child prose, generated identifiers,
    timings, usage and artifact locations. Those fields describe transport or
    narration, not whether a child produced new repository state.
    """

    workspace = _workspace_payload(arguments, result)
    patch_summary_raw = result.get("patch_summary")
    patch_summary = patch_summary_raw if isinstance(patch_summary_raw, dict) else {}
    semantic_signal = result.get("semantic_no_progress")
    return {
        "status": _clean_lower(result.get("status") or tool_status or "success"),
        "has_error": bool(result.get("error")),
        "error_code": _clean_lower(result.get("error_code")),
        "failure_category": _clean_lower(result.get("failure_category")),
        "workspace": workspace,
        "paths": _sorted_strings(
            result.get("material_touched_repo_paths"),
            result.get("touched_repo_paths"),
            patch_summary.get("files"),
        ),
        "effects": _sorted_strings(result.get("effects")),
        "patch_sha256": _patch_sha256(result),
        "material_identity_sha256": _clean_lower(result.get("material_identity_sha256")),
        "semantic_no_progress": (semantic_signal if isinstance(semantic_signal, bool) else None),
    }


def _stable_action_identity(
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Return an exact, language-independent identity for weak outcomes.

    Structured patch/workspace evidence is strong enough to compare on its own.
    Read-only children often have no such evidence, so their exact task bytes are
    hashed to prevent unrelated investigations from collapsing into one outcome.
    No language-specific matching or keyword list is involved.
    """

    task = _clean_text(arguments.get("task"))
    return {
        "subagent": _clean_lower(arguments.get("name") or result.get("subagent")),
        "mode": _clean_lower(arguments.get("mode")),
        "workspace_view": _clean_lower(arguments.get("workspace_view") or "shared"),
        "workspace_from_run": _clean_text(arguments.get("workspace_from_run")),
        "task_sha256": hashlib.sha256(task.encode("utf-8", errors="replace")).hexdigest(),
    }


def _has_strong_outcome_identity(stable_outcome: dict[str, Any]) -> bool:
    # A path list or isolated base says where work happened, not what the work
    # was. Only patch content identity is strong enough to compare independent
    # of the delegated task. Evidence-free/no-change outcomes retain the exact
    # action digest so unrelated investigations cannot collapse together.
    if stable_outcome.get("material_identity_sha256"):
        return True
    workspace_raw = stable_outcome.get("workspace")
    workspace = workspace_raw if isinstance(workspace_raw, dict) else {}
    return bool(
        stable_outcome.get("patch_sha256")
        and stable_outcome.get("paths")
        and workspace.get("no_changes") is not True
    )


def _strong_material_identity(stable_outcome: dict[str, Any]) -> dict[str, Any] | None:
    """Return the smallest complete material identity advertised by the child.

    Coordinator material identity already covers base, paths and patch bytes.  It
    therefore takes precedence over incidental envelope differences such as
    effects, status narration or workspace lifecycle labels.  A bare patch hash
    is weaker, so it remains scoped to the reported workspace view and base.
    """

    workspace_raw = stable_outcome.get("workspace")
    workspace = workspace_raw if isinstance(workspace_raw, dict) else {}
    material_identity_sha256 = _clean_lower(stable_outcome.get("material_identity_sha256"))
    if material_identity_sha256:
        return {
            "kind": "material_identity",
            "sha256": material_identity_sha256,
            "workspace_view": _clean_lower(workspace.get("view")),
        }
    patch_sha256 = _clean_lower(stable_outcome.get("patch_sha256"))
    if patch_sha256 and _has_strong_outcome_identity(stable_outcome):
        return {
            "kind": "patch",
            "sha256": patch_sha256,
            "workspace_view": _clean_lower(workspace.get("view")),
            "base_commit": _clean_text(workspace.get("base_commit")),
        }
    return None


def _screened_report_digest(result: dict[str, Any]) -> str:
    """Identify evidence-poor findings without interpreting their language.

    Material outcomes never depend on report prose.  Read-only/no-change results
    have no stronger evidence, however, so omitting their screened report would
    collapse different findings for the same task.  Hashing the exact screened
    text is language-independent and deliberately conservative: paraphrases do
    not become equivalent unless the coordinator supplies structured identity.
    """

    for key in ("result", "final_text", "partial_report"):
        value = result.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text:
            return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return ""


def subagent_outcome_has_material_identity(result: dict[str, Any]) -> bool:
    stable = _stable_subagent_outcome(arguments={}, result=result, tool_status="done")
    return _has_strong_outcome_identity(stable)


def subagent_semantic_outcome_fingerprint(
    *,
    arguments: dict[str, Any],
    result: dict[str, Any],
    tool_status: str,
) -> str:
    stable_outcome = _stable_subagent_outcome(
        arguments=arguments,
        result=result,
        tool_status=tool_status,
    )
    # The coordinator signal decides whether this occurrence is progress; it is
    # not itself identity.  Strong material identity must dominate incidental
    # envelope differences so a canonical capture and its duplicate coalesce.
    material_identity = _strong_material_identity(stable_outcome)
    if material_identity is not None:
        payload: dict[str, Any] = {"material": material_identity}
    else:
        identity_outcome = dict(stable_outcome)
        identity_outcome.pop("semantic_no_progress", None)
        identity_outcome.pop("effects", None)
        payload = {
            "outcome": identity_outcome,
            "action": _stable_action_identity(arguments, result),
            "report_sha256": _screened_report_digest(result),
        }
    return _sha256_json(payload)


def subagent_launch_intent_fingerprint(arguments: dict[str, Any]) -> str:
    """Return the exact typed objective identity of one background launch.

    Caller-selected run ids, display labels and operational limits are transport
    choices, not new delegation objectives.  The projection therefore keeps only
    the protocol fields that determine *what* child is being launched and what
    workspace/dependency inputs it receives. Presentation-only whitespace is
    collapsed before hashing; no language-specific task classification or
    case-folding is performed.
    """

    task = _normalized_launch_task(arguments.get("task"))
    raw_dependencies = arguments.get("depends_on")
    if isinstance(raw_dependencies, (list, tuple, set, frozenset)):
        dependencies: tuple[str, ...] = tuple(
            sorted(str(value).strip() for value in raw_dependencies)
        )
    elif raw_dependencies is None:
        dependencies = ()
    else:
        # Schema validation normally rejects this before dispatch.  Retaining an
        # invalid typed value is safer than collapsing it with an empty list.
        dependencies = (f"{type(raw_dependencies).__name__}:{raw_dependencies!s}",)
    payload = {
        "subagent_profile": _clean_lower(arguments.get("name")),
        "task_sha256": hashlib.sha256(task.encode("utf-8", errors="replace")).hexdigest(),
        "mode": _clean_lower(arguments.get("mode")),
        "workspace_view": _clean_lower(arguments.get("workspace_view") or "shared"),
        "workspace_from_run": _clean_text(arguments.get("workspace_from_run")),
        "depends_on": dependencies,
    }
    return _sha256_json(payload)


_SYNCHRONOUS_LIFECYCLE_TOOLS = frozenset(
    {
        "subagent_apply",
    }
)


def _subagent_lifecycle_operation_fingerprint(
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Identify one typed synchronous lifecycle operation exactly.

    Lifecycle replay identity is protocol data: the normalized tool name and
    its complete typed argument object.  It deliberately does not inspect
    prompts, infer intent from prose, or special-case particular run ids.
    """

    return _sha256_json(
        {
            "tool_name": _clean_lower(tool_name),
            "arguments": arguments,
        }
    )


def _successful_subagent_lifecycle_noop(
    *,
    tool_name: str,
    result: dict[str, Any],
    tool_status: str,
) -> bool:
    """Whether a lifecycle result is a successful, terminal no-op.

    Failed results and incomplete cleanup are intentionally excluded so an
    identical retry can still dispatch.  Each supported operation is matched
    through its typed result fields rather than a shared prose heuristic.
    """

    normalized_tool = _clean_lower(tool_name)
    if normalized_tool not in _SYNCHRONOUS_LIFECYCLE_TOOLS:
        return False
    if (
        _clean_lower(tool_status) == "failed"
        or result.get("ok") is not True
        or bool(result.get("error"))
        or bool(_clean_text(result.get("error_code")))
        or result.get("semantic_no_progress") is not True
        or result.get("cleanup_pending") is True
        or result.get("physical_worktree_removed") is False
    ):
        return False
    action = _clean_lower(result.get("action"))
    return bool(
        action == "already_integrated"
        and result.get("already_integrated") is True
        and not result.get("applied_paths")
    )


def _successful_subagent_discard(
    *,
    tool_name: str,
    result: dict[str, Any],
    tool_status: str,
) -> bool:
    """Whether the provider accepted a typed discard lifecycle transition."""

    return bool(
        _clean_lower(tool_name) == "subagent_discard"
        and _clean_lower(tool_status) != "failed"
        and result.get("ok") is True
        and _clean_lower(result.get("action")) == "discarded"
        and not bool(result.get("error"))
        and not bool(_clean_text(result.get("error_code")))
    )


@dataclass(frozen=True)
class RootSubagentLifecycleReplayDecision:
    """Host decision for a repeated synchronous lifecycle no-op."""

    tool_name: str
    fingerprint: str
    objective_revision: int
    allow_dispatch: bool
    canonical_result: dict[str, Any] | None
    reason: str

    @property
    def stop_signal(self) -> bool:
        return not self.allow_dispatch

    def telemetry_payload(self) -> dict[str, Any]:
        return {
            "lifecycle_tool": self.tool_name,
            "operation_fingerprint_prefix": self.fingerprint[:12],
            "objective_revision": self.objective_revision,
            "semantic_no_progress": not self.allow_dispatch,
            "stop_signal": self.stop_signal,
            "coalesced": self.canonical_result is not None,
            "reason": self.reason,
        }

    def stopped_result(self) -> dict[str, Any]:
        if self.allow_dispatch:
            raise ValueError("lifecycle replay decision allows dispatch")
        if self.canonical_result is None:
            raise ValueError("lifecycle replay decision has no canonical result")
        result = copy.deepcopy(self.canonical_result)
        result.update(
            {
                "dispatch_blocked": True,
                "coalesced": True,
                "semantic_no_progress": True,
                "status": "blocked",
                "error_code": "equivalent_subagent_lifecycle_replay",
                "message": (
                    "This identical subagent lifecycle operation already returned a "
                    "successful no-op in the current parent state."
                ),
                "lifecycle_guard": self.telemetry_payload(),
            }
        )
        return result


@dataclass(frozen=True)
class RootSubagentForegroundReplayDecision:
    """Host decision for a sequential foreground launch in one parent turn."""

    fingerprint: str
    objective_revision: int
    allow_dispatch: bool
    semantic_no_progress: bool
    stop_signal: bool
    canonical_result: dict[str, Any] | None
    reason: str

    def telemetry_payload(self) -> dict[str, Any]:
        return {
            "launch_fingerprint_prefix": self.fingerprint[:12],
            "objective_revision": self.objective_revision,
            "semantic_no_progress": self.semantic_no_progress,
            "stop_signal": self.stop_signal,
            "coalesced": self.canonical_result is not None,
            "reason": self.reason,
        }

    def coalesced_result(self) -> dict[str, Any]:
        if self.allow_dispatch:
            raise ValueError("foreground replay decision allows dispatch")
        if self.canonical_result is None:
            raise ValueError("foreground replay decision has no canonical result")
        result = copy.deepcopy(self.canonical_result)
        canonical_run_id = _clean_text(result.get("canonical_run_id") or result.get("run_id"))
        result.update(
            {
                "ok": True,
                "dispatch_blocked": True,
                "coalesced": True,
                "semantic_no_progress": True,
                "duplicate_of": canonical_run_id,
                "canonical_run_id": canonical_run_id,
                "launch_guard": self.telemetry_payload(),
            }
        )
        workspace_raw = result.get("workspace")
        if isinstance(workspace_raw, dict):
            result["workspace"] = {
                **workspace_raw,
                "semantic_no_progress": True,
                "duplicate_of": canonical_run_id,
                "canonical_run_id": canonical_run_id,
            }
        return result

    def stopped_result(self) -> dict[str, Any]:
        result = self.coalesced_result()
        result.update(
            {
                "status": "blocked",
                "error_code": "equivalent_foreground_subagent_replay",
                "message": (
                    "This equivalent foreground subagent objective already produced "
                    "a structured no-progress outcome in the current parent state."
                ),
            }
        )
        return result


@dataclass(frozen=True)
class RootSubagentLaunchDecision:
    """Host decision for an exact same-turn background launch objective."""

    fingerprint: str
    generation: int
    canonical_generation: int
    allow_dispatch: bool
    requested_run_id: str
    canonical_run_id: str
    coalesced: bool
    semantic_no_progress: bool
    stop_signal: bool
    reason: str

    def telemetry_payload(self) -> dict[str, Any]:
        return {
            "launch_fingerprint_prefix": self.fingerprint[:12],
            "launch_generation": self.generation,
            "canonical_generation": self.canonical_generation,
            "requested_run_id": self.requested_run_id,
            "canonical_run_id": self.canonical_run_id,
            "coalesced": self.coalesced,
            "semantic_no_progress": self.semantic_no_progress,
            "stop_signal": self.stop_signal,
            "reason": self.reason,
        }

    def coalesced_result(self) -> dict[str, Any]:
        if not self.coalesced or not self.canonical_run_id:
            raise ValueError("launch decision does not identify a canonical run")
        return {
            "ok": True,
            "run_id": self.canonical_run_id,
            "duplicate_of": self.canonical_run_id,
            "coalesced": True,
            "semantic_no_progress": True,
            "launch_guard": self.telemetry_payload(),
        }


class RootSubagentLaunchGuard:
    """Coalesce exact background-launch objectives within one parent turn.

    This guard deliberately has no step, time or occurrence limit.  Its history
    is scoped to the parent turn and is independent of material objective
    revisions: editing the parent workspace does not make relabelling an already
    launched child into a new delegation objective.
    """

    _DUPLICATE_RUN_ID_ERROR_CODES = frozenset(
        {
            "duplicate_background_subagent_run_id",
        }
    )

    def __init__(self) -> None:
        self._canonical_run_by_fingerprint: dict[str, str] = {}
        self._canonical_generation_by_fingerprint: dict[str, int] = {}
        self._fingerprint_by_run_id: dict[str, str] = {}

    def inspect_spawn(
        self,
        arguments: dict[str, Any],
        *,
        generation: int,
    ) -> RootSubagentLaunchDecision:
        fingerprint = subagent_launch_intent_fingerprint(arguments)
        requested_run_id = _clean_text(arguments.get("run_id"))
        canonical_run_id = self._canonical_run_by_fingerprint.get(fingerprint, "")
        canonical_generation = self._canonical_generation_by_fingerprint.get(fingerprint, -1)
        if not canonical_run_id:
            return RootSubagentLaunchDecision(
                fingerprint=fingerprint,
                generation=generation,
                canonical_generation=-1,
                allow_dispatch=True,
                requested_run_id=requested_run_id,
                canonical_run_id="",
                coalesced=False,
                semantic_no_progress=False,
                stop_signal=False,
                reason="new_launch_intent",
            )

        known_requested_fingerprint = self._fingerprint_by_run_id.get(requested_run_id)
        repeats_known_run = bool(requested_run_id and known_requested_fingerprint == fingerprint)
        if repeats_known_run:
            return RootSubagentLaunchDecision(
                fingerprint=fingerprint,
                generation=generation,
                canonical_generation=canonical_generation,
                allow_dispatch=False,
                requested_run_id=requested_run_id,
                canonical_run_id=requested_run_id,
                coalesced=True,
                semantic_no_progress=True,
                stop_signal=generation != canonical_generation,
                reason=(
                    "launch_intent_repeated_after_generation"
                    if generation != canonical_generation
                    else "same_generation_run_id_duplicate"
                ),
            )

        if generation == canonical_generation:
            # Multiple children in one assistant tool batch may intentionally
            # receive the same objective (for example, redundant reviewers).
            # Their shared generation is structural evidence of deliberate
            # fan-out; a later model round is a sequential replay instead.
            return RootSubagentLaunchDecision(
                fingerprint=fingerprint,
                generation=generation,
                canonical_generation=canonical_generation,
                allow_dispatch=True,
                requested_run_id=requested_run_id,
                canonical_run_id=canonical_run_id,
                coalesced=False,
                semantic_no_progress=False,
                stop_signal=False,
                reason="same_generation_replica",
            )

        return RootSubagentLaunchDecision(
            fingerprint=fingerprint,
            generation=generation,
            canonical_generation=canonical_generation,
            allow_dispatch=False,
            requested_run_id=requested_run_id,
            canonical_run_id=canonical_run_id,
            coalesced=True,
            semantic_no_progress=True,
            stop_signal=True,
            reason="launch_intent_repeated_after_generation",
        )

    def record_spawn_result(
        self,
        *,
        arguments: dict[str, Any],
        result: dict[str, Any],
        tool_status: str,
        generation: int,
    ) -> None:
        """Record a scheduler result using structured fields only."""

        fingerprint = subagent_launch_intent_fingerprint(arguments)
        requested_run_id = _clean_text(arguments.get("run_id"))
        error_code = _clean_lower(result.get("error_code"))
        if error_code in self._DUPLICATE_RUN_ID_ERROR_CODES:
            return
        if _clean_lower(tool_status) == "failed" or bool(result.get("error")) or error_code:
            return
        run_id = _clean_text(result.get("run_id") or requested_run_id)
        if not run_id:
            return
        canonical_run_id = self._canonical_run_by_fingerprint.setdefault(fingerprint, run_id)
        self._canonical_generation_by_fingerprint.setdefault(fingerprint, generation)
        self._fingerprint_by_run_id.setdefault(canonical_run_id, fingerprint)
        self._fingerprint_by_run_id.setdefault(run_id, fingerprint)


def _successful_subagent_outcome(result: dict[str, Any], tool_status: str) -> bool:
    # The coordinator may retain a valid structured no-progress decision while
    # degrading the child envelope (for example, a writable child that produced
    # no workspace delta but claimed completion).  Preserve that decision; an
    # ordinary failed child without this signal is not semantic progress data.
    if result.get("semantic_no_progress") is True:
        return True
    if _clean_lower(tool_status) == "failed" or bool(result.get("error")):
        return False
    result_status = _clean_lower(result.get("status"))
    return result_status in {"", "success", "done", "completed"}


def terminal_captured_duplicate_subagent_run(
    *,
    tool_name: str,
    result: dict[str, Any],
    tool_status: str,
) -> bool:
    """Whether one foreground result is a complete captured-duplicate transition.

    This is intentionally narrower than ``semantic_no_progress``.  A duplicate
    of an applied candidate still needs a lifecycle operation, and a duplicate
    whose physical cleanup failed still needs recovery.  Only the coordinator's
    complete typed envelope can close the root turn; task prose and generated
    identifiers never participate in the decision.
    """

    if _clean_lower(tool_name) != "subagent_run":
        return False
    if (
        _clean_lower(tool_status) == "failed"
        or result.get("ok") is False
        or bool(result.get("error"))
        or bool(_clean_text(result.get("error_code")))
        or _clean_lower(result.get("status")) not in {"", "success", "done", "completed"}
    ):
        return False
    if (
        result.get("semantic_no_progress") is not True
        or result.get("already_integrated") is not False
        or _clean_lower(result.get("canonical_state")) != "captured"
        or result.get("cleanup_pending") is not False
        or result.get("physical_worktree_removed") is not True
        or result.get("candidate_worktree_retained") is not False
        or not subagent_outcome_has_material_identity(result)
    ):
        return False
    run_id = _clean_text(result.get("run_id"))
    duplicate_of = _clean_text(result.get("duplicate_of"))
    canonical_run_id = _clean_text(result.get("canonical_run_id"))
    if not run_id or not duplicate_of or not canonical_run_id:
        return False
    if run_id == canonical_run_id or duplicate_of != canonical_run_id:
        return False
    workspace_raw = result.get("workspace")
    workspace = workspace_raw if isinstance(workspace_raw, dict) else {}
    return _clean_lower(workspace.get("view") or result.get("workspace_view")) == "isolated"


SUBAGENT_LIFECYCLE_CAPSULE_SCHEMA = "alysis.subagent-lifecycle-capsule/v1"


def captured_duplicate_subagent_lifecycle_capsule(
    *,
    tool_name: str,
    result: dict[str, Any],
    tool_status: str,
) -> dict[str, Any] | None:
    """Build the allowlisted lifecycle state retained after a TUI history rollover.

    The capsule deliberately excludes task text, child prose, artifact paths,
    timings, usage, and provider metadata.  It is produced only from the same
    strict typed transition that can terminalize the parent turn.
    """

    if not terminal_captured_duplicate_subagent_run(
        tool_name=tool_name,
        result=result,
        tool_status=tool_status,
    ):
        return None
    material_identity_sha256 = _clean_lower(result.get("material_identity_sha256"))
    if len(material_identity_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in material_identity_sha256
    ):
        return None
    canonical_run_id = _clean_text(result.get("canonical_run_id"))
    return {
        "schema": SUBAGENT_LIFECYCLE_CAPSULE_SCHEMA,
        "previous_result": {
            "tool": "subagent_run",
            "run_id": _clean_text(result.get("run_id")),
            "state": "duplicate",
            "duplicate_of": canonical_run_id,
            "canonical_run_id": canonical_run_id,
            "canonical_state": "captured",
            "material_identity_sha256": material_identity_sha256,
            "already_integrated": False,
            "cleanup_pending": False,
            "physical_worktree_removed": True,
        },
    }


def validate_subagent_lifecycle_capsule(capsule: dict[str, Any]) -> None:
    """Reject any capsule outside the exact production allowlist."""

    if set(capsule) != {"schema", "previous_result"} or (
        capsule.get("schema") != SUBAGENT_LIFECYCLE_CAPSULE_SCHEMA
    ):
        raise ValueError("unsupported subagent lifecycle capsule schema")
    previous_result = capsule.get("previous_result")
    if not isinstance(previous_result, dict):
        raise ValueError("subagent lifecycle capsule is missing previous_result")
    expected_result_keys = {
        "tool",
        "run_id",
        "state",
        "duplicate_of",
        "canonical_run_id",
        "canonical_state",
        "material_identity_sha256",
        "already_integrated",
        "cleanup_pending",
        "physical_worktree_removed",
    }
    run_id = _clean_text(previous_result.get("run_id"))
    duplicate_of = _clean_text(previous_result.get("duplicate_of"))
    canonical_run_id = _clean_text(previous_result.get("canonical_run_id"))
    material_identity_sha256 = _clean_lower(previous_result.get("material_identity_sha256"))
    if (
        set(previous_result) != expected_result_keys
        or previous_result.get("tool") != "subagent_run"
        or previous_result.get("state") != "duplicate"
        or previous_result.get("canonical_state") != "captured"
        or previous_result.get("already_integrated") is not False
        or previous_result.get("cleanup_pending") is not False
        or previous_result.get("physical_worktree_removed") is not True
        or not run_id
        or not duplicate_of
        or duplicate_of != canonical_run_id
        or run_id == canonical_run_id
        or len(material_identity_sha256) != 64
        or any(character not in "0123456789abcdef" for character in material_identity_sha256)
    ):
        raise ValueError("invalid subagent lifecycle capsule payload")


def render_subagent_lifecycle_capsule(capsule: dict[str, Any]) -> str:
    """Render one neutral, machine-readable system message for model history."""

    validate_subagent_lifecycle_capsule(capsule)
    canonical = json.dumps(
        capsule,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f'<subagent_lifecycle_capsule schema="{SUBAGENT_LIFECYCLE_CAPSULE_SCHEMA}">\n'
        f"{canonical}\n"
        "</subagent_lifecycle_capsule>"
    )


@dataclass(frozen=True)
class RootSubagentProgressObservation:
    fingerprint: str
    occurrences: int
    fingerprint_occurrences: int
    objective_revision: int
    semantic_no_progress: bool
    coordinator_signal: bool | None
    workspace_view: str
    paths: tuple[str, ...]
    patch_sha256: str

    def telemetry_payload(self) -> dict[str, Any]:
        return {
            "fingerprint_prefix": self.fingerprint[:12],
            "occurrences": self.occurrences,
            "fingerprint_occurrences": self.fingerprint_occurrences,
            "objective_revision": self.objective_revision,
            "semantic_no_progress": self.semantic_no_progress,
            "coordinator_signal": self.coordinator_signal,
            "workspace_view": self.workspace_view,
            "paths": list(self.paths),
            "patch_sha256_prefix": self.patch_sha256[:12],
        }


class RootSubagentProgressGuard:
    """Detect equivalent root-child outcomes without imposing a run limit."""

    def __init__(self, *, recent_window: int) -> None:
        self._objective_revision = 0
        self._recent: deque[tuple[int, str]] = deque(maxlen=max(2, int(recent_window)))
        self._no_progress_streak = 0
        self._foreground_canonical_results: dict[tuple[int, str], dict[str, Any]] = {}
        self._foreground_no_progress_intents: set[tuple[int, str]] = set()
        self._lifecycle_noop_results: dict[tuple[int, str], dict[str, Any]] = {}

    @property
    def objective_revision(self) -> int:
        return self._objective_revision

    def note_objective_transition(self) -> None:
        self._objective_revision += 1
        self._no_progress_streak = 0
        self._foreground_canonical_results.clear()
        self._foreground_no_progress_intents.clear()
        self._lifecycle_noop_results.clear()

    def inspect_lifecycle_operation(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> RootSubagentLifecycleReplayDecision | None:
        """Stop an exact operation already proven to be a terminal no-op."""

        normalized_tool = _clean_lower(tool_name)
        if normalized_tool not in _SYNCHRONOUS_LIFECYCLE_TOOLS:
            return None
        fingerprint = _subagent_lifecycle_operation_fingerprint(
            tool_name=normalized_tool,
            arguments=arguments,
        )
        canonical_result = self._lifecycle_noop_results.get((self._objective_revision, fingerprint))
        return RootSubagentLifecycleReplayDecision(
            tool_name=normalized_tool,
            fingerprint=fingerprint,
            objective_revision=self._objective_revision,
            allow_dispatch=canonical_result is None,
            canonical_result=(
                copy.deepcopy(canonical_result) if canonical_result is not None else None
            ),
            reason=(
                "lifecycle_noop_repeated_without_objective_transition"
                if canonical_result is not None
                else "lifecycle_operation_available"
            ),
        )

    def record_lifecycle_result(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        tool_status: str,
    ) -> None:
        """Track terminal apply no-ops and invalidate them after a discard."""

        if _successful_subagent_discard(
            tool_name=tool_name,
            result=result,
            tool_status=tool_status,
        ):
            # Alias/canonical relationships are provider-owned and are not part
            # of apply arguments. Clear the small revision-scoped cache rather
            # than guessing which retained aliases the discard invalidated.
            self._lifecycle_noop_results.clear()
            return

        if not _successful_subagent_lifecycle_noop(
            tool_name=tool_name,
            result=result,
            tool_status=tool_status,
        ):
            return
        fingerprint = _subagent_lifecycle_operation_fingerprint(
            tool_name=tool_name,
            arguments=arguments,
        )
        self._lifecycle_noop_results.setdefault(
            (self._objective_revision, fingerprint),
            copy.deepcopy(result),
        )

    def inspect_foreground_launch(
        self,
        arguments: dict[str, Any],
    ) -> RootSubagentForegroundReplayDecision:
        """Coalesce an exact replay, then refuse repetition of that outcome.

        A first launch and any retry after a failed launch are allowed. The first
        sequential replay reuses the complete canonical result without opening
        another child. Once that structured no-progress result has been observed,
        another equivalent attempt stops the turn. A changed task or real
        parent-state transition remains dispatchable.
        """

        fingerprint = subagent_launch_intent_fingerprint(arguments)
        key = (self._objective_revision, fingerprint)
        canonical_result = self._foreground_canonical_results.get(key)
        replayed = key in self._foreground_no_progress_intents
        coalesced = canonical_result is not None
        return RootSubagentForegroundReplayDecision(
            fingerprint=fingerprint,
            objective_revision=self._objective_revision,
            allow_dispatch=not coalesced,
            semantic_no_progress=coalesced,
            stop_signal=replayed,
            canonical_result=(copy.deepcopy(canonical_result) if coalesced else None),
            reason=(
                "foreground_launch_repeated_after_semantic_no_progress"
                if replayed
                else (
                    "foreground_launch_coalesced_with_canonical_result"
                    if coalesced
                    else "foreground_launch_intent_available"
                )
            ),
        )

    def record_foreground_result(
        self,
        *,
        arguments: dict[str, Any],
        result: dict[str, Any],
        tool_status: str,
        semantic_no_progress: bool,
    ) -> None:
        """Record a parent-projected synchronous result for replay containment."""

        if not _successful_subagent_outcome(result, tool_status):
            return
        fingerprint = subagent_launch_intent_fingerprint(arguments)
        key = (self._objective_revision, fingerprint)
        self._foreground_canonical_results.setdefault(key, copy.deepcopy(result))
        if semantic_no_progress:
            self._foreground_no_progress_intents.add(key)

    def observe(
        self,
        *,
        arguments: dict[str, Any],
        result: dict[str, Any],
        tool_status: str,
    ) -> RootSubagentProgressObservation | None:
        observations = self.observe_batch(
            outcomes=((arguments, result, tool_status),),
        )
        return observations[0] if observations else None

    def observe_batch(
        self,
        *,
        outcomes: tuple[tuple[dict[str, Any], dict[str, Any], str], ...],
    ) -> tuple[RootSubagentProgressObservation, ...]:
        """Observe one parent tool result, possibly containing several children.

        Identity counts remain per child, but containment occurrences advance at
        most once for this parent orchestration outcome.  This prevents one
        intentional background batch from consuming the entire repetition
        policy in a single ``subagent_wait`` call.
        """

        provisional: list[tuple[RootSubagentProgressObservation, int]] = []
        occurrences_within_batch: dict[tuple[int, str], int] = {}
        for arguments, result, tool_status in outcomes:
            if not _successful_subagent_outcome(result, tool_status):
                continue
            fingerprint = subagent_semantic_outcome_fingerprint(
                arguments=arguments,
                result=result,
                tool_status=tool_status,
            )
            key = (self._objective_revision, fingerprint)
            prior_batch_occurrences = self._recent.count(key)
            within_batch = occurrences_within_batch.get(key, 0)
            occurrences_within_batch[key] = within_batch + 1
            fingerprint_occurrences = prior_batch_occurrences + within_batch + 1
            raw_signal = result.get("semantic_no_progress")
            coordinator_signal = raw_signal if isinstance(raw_signal, bool) else None
            if coordinator_signal is None:
                semantic_no_progress = fingerprint_occurrences > 1
            else:
                # An explicit coordinator decision is authoritative. In
                # particular, False means a real state transition even if the
                # compact structured result resembles an earlier one.
                semantic_no_progress = coordinator_signal
            stable = _stable_subagent_outcome(
                arguments=arguments,
                result=result,
                tool_status=tool_status,
            )
            workspace = stable.get("workspace")
            provisional.append(
                (
                    RootSubagentProgressObservation(
                        fingerprint=fingerprint,
                        occurrences=prior_batch_occurrences + 1,
                        fingerprint_occurrences=fingerprint_occurrences,
                        objective_revision=self._objective_revision,
                        semantic_no_progress=semantic_no_progress,
                        coordinator_signal=coordinator_signal,
                        workspace_view=(
                            _clean_lower(workspace.get("view"))
                            if isinstance(workspace, dict)
                            else ""
                        ),
                        paths=tuple(stable.get("paths") or ()),
                        patch_sha256=_clean_lower(stable.get("patch_sha256")),
                    ),
                    prior_batch_occurrences + 1,
                )
            )

        if not provisional:
            return ()
        for key in occurrences_within_batch:
            self._recent.append(key)
        batch_semantic_no_progress = all(
            observation.semantic_no_progress for observation, _ in provisional
        )
        if batch_semantic_no_progress:
            self._no_progress_streak += 1
        else:
            self._no_progress_streak = 0
        return tuple(
            replace(
                observation,
                occurrences=max(batch_occurrences, self._no_progress_streak),
            )
            for observation, batch_occurrences in provisional
        )


def tool_result_advances_root_objective_state(
    *,
    tool_name: str,
    tool_status: str,
    result: dict[str, Any],
    action_progress: bool,
    subagent_observations: tuple[RootSubagentProgressObservation, ...],
) -> bool:
    """Whether a completed tool changed objective state visible to the root.

    Isolated child work is a candidate, not a parent-workspace transition.
    Applying or discarding that candidate is a transition. The decision uses
    only tool protocol fields, never natural-language interpretation.
    """

    if _clean_lower(tool_status) == "failed" or bool(result.get("error")):
        return False
    semantic_signal = result.get("semantic_no_progress")
    if semantic_signal is True:
        return False
    normalized_tool = _clean_lower(tool_name)
    if normalized_tool in {"subagent_run", "subagent_wait"}:
        fresh_observations = tuple(
            observation
            for observation in subagent_observations
            if not observation.semantic_no_progress
        )
        return bool(
            action_progress
            and fresh_observations
            and any(observation.workspace_view != "isolated" for observation in fresh_observations)
        )
    if normalized_tool == "subagent_spawn":
        # Spawn acknowledgement is lifecycle state, not completed child work.
        return False
    if normalized_tool == "subagent_apply":
        return bool(
            result.get("ok") is True and (result.get("applied_paths") or semantic_signal is False)
        )
    if normalized_tool == "subagent_discard":
        return bool(
            result.get("ok") is True
            and _clean_lower(result.get("action")) == "discarded"
            and not _clean_text(result.get("duplicate_of"))
            and (result.get("paths") or semantic_signal is False)
        )
    return bool(action_progress)
