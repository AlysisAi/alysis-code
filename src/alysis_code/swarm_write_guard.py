"""Swarm-layer write-scope path guard (defense in depth).

The agent layer already guards writes (deny prefixes + allow globs inside
``agent/tools_assembly.py``), but from the swarm's perspective that is an
honor system: a bug there would let a worker write outside its task's
``write_scope`` and silently poison sibling tasks or the merge. This module
is the swarm-owned second gate, applied structurally at the tool dispatch
boundary (every ``ToolDef.run`` is wrapped), so it holds even when the
agent-layer checks are buggy or bypassed.

Policy:
- WRITES must stay inside the task's normalized ``write_scope`` patterns
  (plus the run-scoped scratch/temp roots); READS are never guarded.
- Violations are recorded as typed :class:`WriteScopeViolation` records and
  surface as a tool error to the model; the swarm worker fails the task
  closed when any violation was recorded, regardless of the agent exit code.
- Hardening: raw ``..`` traversal is rejected before resolution, containment
  is re-verified with ``Path.resolve()`` (realpath) independently of the
  tool's own resolution, writes through symlinked components are rejected
  (a symlink inside the worktree silently rewrites which relative path gets
  scope-checked), and matching is case-insensitive on win32-style
  case-insensitive filesystems.

Out of scope (documented, unchanged): ``shell_run``/``shell_background``
cannot be path-guarded at dispatch ? the sandbox profile and the post-run
``assess_scope_changes`` pass remain the controls for shell writes.
"""

from __future__ import annotations

import fnmatch
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .agent.errors import AgentRuntimeError
from .diff_paths import iter_patch_paths
from .task_scope import (
    ancestor_directory_scope_patterns,
    is_non_material_untracked_path,
    scope_path_matches_pattern,
)

# Tool name -> ((path_field, base_field), ...) write targets. fs_copy guards
# only the destination and fs_move guards both ends, mirroring the agent
# layer's contract.
_WRITE_TOOL_PATH_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "fs_write": (("path", "path_base"),),
    "fs_edit": (("path", "path_base"),),
    "fs_mkdir": (("path", "path_base"),),
    "fs_delete": (("path", "path_base"),),
    "fs_move": (
        ("source_path", "source_path_base"),
        ("destination_path", "destination_path_base"),
    ),
    "fs_copy": (("destination_path", "destination_path_base"),),
}
_PATCH_TOOL_NAMES = frozenset({"git_apply_patch"})


class SwarmWriteScopeViolationError(AgentRuntimeError):
    """Raised at the tool dispatch boundary for an out-of-scope write.

    Subclasses ``AgentRuntimeError`` so the turn engine degrades it to a
    structured tool error the model can react to; the recorded violation
    still fails the task at the swarm layer afterwards.
    """


@dataclass(frozen=True, slots=True)
class WriteScopeViolation:
    tool_name: str
    raw_path: str
    reason: str
    detail: str
    resolved_relpath: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "write_scope_guard_violation",
            "tool": self.tool_name,
            "path": self.raw_path,
            "resolved_relpath": self.resolved_relpath,
            "reason": self.reason,
            "detail": self.detail,
        }


def _normalize_slashes(value: str) -> str:
    return value.replace("\\", "/").strip()


class SwarmWriteScopeGuard:
    """Per-task hard write guard keyed by write_scope + run-scoped temp roots."""

    def __init__(
        self,
        *,
        worktree_root: Path,
        allowed_patterns: Sequence[str],
        extra_allowed_roots: Sequence[Path] = (),
        case_insensitive: bool | None = None,
    ) -> None:
        self.worktree_root = Path(worktree_root).resolve()
        self.allowed_patterns = tuple(
            str(item).strip() for item in allowed_patterns if str(item).strip()
        )
        self.extra_allowed_roots = tuple(Path(item).resolve() for item in extra_allowed_roots)
        self.case_insensitive = (
            sys.platform == "win32" if case_insensitive is None else bool(case_insensitive)
        )
        self._ancestor_dirs_cf = {
            _normalize_slashes(path).casefold()
            for path in ancestor_directory_scope_patterns(
                list(self.allowed_patterns), root=self.worktree_root
            )
        }
        self._lock = threading.Lock()
        self._violations: list[WriteScopeViolation] = []

    @property
    def violations(self) -> list[WriteScopeViolation]:
        with self._lock:
            return list(self._violations)

    def check_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        resolve_rel_path: Callable[..., str] | None = None,
    ) -> None:
        """Veto an out-of-scope write before the tool runs.

        ``resolve_rel_path`` is the live tool-layer resolver (active-workdir
        aware); when provided it determines what the tool would actually
        write, and the guard independently re-verifies containment on top.
        """
        for raw_path, resolved_rel in self._iter_write_targets(
            tool_name, arguments, resolve_rel_path=resolve_rel_path
        ):
            violation = self._check_target(
                tool_name=tool_name,
                raw_path=raw_path,
                resolved_rel=resolved_rel,
            )
            if violation is not None:
                with self._lock:
                    self._violations.append(violation)
                raise SwarmWriteScopeViolationError(
                    "Write blocked by the swarm write-scope guard "
                    f"({violation.reason}): {violation.raw_path}. {violation.detail}"
                )

    def _iter_write_targets(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        resolve_rel_path: Callable[..., str] | None,
    ) -> Iterator[tuple[str, str | None]]:
        if tool_name in _PATCH_TOOL_NAMES:
            patch_text = str(arguments.get("patch") or "")
            for path in iter_patch_paths(patch_text):
                yield path, None
            return
        fields = _WRITE_TOOL_PATH_FIELDS.get(tool_name)
        if fields is None:
            return
        for path_field, base_field in fields:
            raw_path = arguments.get(path_field)
            if raw_path is None or not str(raw_path).strip():
                continue
            resolved_rel: str | None = None
            if resolve_rel_path is not None:
                try:
                    resolved_rel = resolve_rel_path(
                        raw_path=raw_path,
                        raw_base=arguments.get(base_field),
                        field_name=path_field,
                        base_field_name=base_field,
                    )
                except AgentRuntimeError as exc:
                    violation = WriteScopeViolation(
                        tool_name=tool_name,
                        raw_path=str(raw_path),
                        reason="path_escape",
                        detail=f"tool path resolution rejected the target: {exc}",
                    )
                    with self._lock:
                        self._violations.append(violation)
                    raise SwarmWriteScopeViolationError(
                        "Write blocked by the swarm write-scope guard "
                        f"(path_escape): {raw_path}. {exc}"
                    ) from exc
            yield str(raw_path), resolved_rel

    def _check_target(
        self,
        *,
        tool_name: str,
        raw_path: str,
        resolved_rel: str | None,
    ) -> WriteScopeViolation | None:
        raw_text = _normalize_slashes(str(raw_path))
        if "\x00" in raw_text:
            return WriteScopeViolation(
                tool_name=tool_name,
                raw_path=str(raw_path),
                reason="invalid_path",
                detail="write target contains a NUL byte.",
            )
        if any(part == ".." for part in PurePosixPath(raw_text).parts):
            return WriteScopeViolation(
                tool_name=tool_name,
                raw_path=str(raw_path),
                reason="parent_traversal",
                detail="write targets must not contain '..' components.",
            )

        raw_as_path = Path(raw_text)
        if raw_as_path.is_absolute():
            resolved_abs = raw_as_path.resolve()
            if self._is_under_extra_allowed_root(resolved_abs):
                return None
            rel = self._relative_to_root(resolved_abs)
            if rel is None:
                return WriteScopeViolation(
                    tool_name=tool_name,
                    raw_path=str(raw_path),
                    reason="path_escape",
                    detail="absolute write target resolves outside the task worktree.",
                )
        else:
            rel = _normalize_slashes(resolved_rel) if resolved_rel else raw_text
            while rel.startswith("./"):
                rel = rel[2:]

        candidate = self.worktree_root / rel
        symlink_component = self._first_symlink_component(rel)
        if symlink_component is not None:
            return WriteScopeViolation(
                tool_name=tool_name,
                raw_path=str(raw_path),
                resolved_relpath=rel,
                reason="symlink_component",
                detail=(
                    "write path passes through a symlink "
                    f"({symlink_component}); symlinked write targets are rejected."
                ),
            )
        resolved_candidate = candidate.resolve()
        if self._is_under_extra_allowed_root(resolved_candidate):
            return None
        if self._relative_to_root(resolved_candidate) is None:
            return WriteScopeViolation(
                tool_name=tool_name,
                raw_path=str(raw_path),
                resolved_relpath=rel,
                reason="path_escape",
                detail="write target resolves outside the task worktree.",
            )

        if self._rel_path_in_scope(rel, tool_name=tool_name):
            return None
        scope_preview = ", ".join(self.allowed_patterns[:8]) or "(empty write scope)"
        return WriteScopeViolation(
            tool_name=tool_name,
            raw_path=str(raw_path),
            resolved_relpath=rel,
            reason="out_of_scope",
            detail=f"write target is outside the task write scope: {scope_preview}.",
        )

    def _relative_to_root(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.worktree_root).as_posix()
        except ValueError:
            if not self.case_insensitive:
                return None
            path_cf = str(path).replace("\\", "/").casefold()
            root_cf = str(self.worktree_root).replace("\\", "/").casefold().rstrip("/")
            if path_cf == root_cf:
                return "."
            if path_cf.startswith(root_cf + "/"):
                return str(path).replace("\\", "/")[len(root_cf) + 1 :]
            return None

    def _is_under_extra_allowed_root(self, resolved: Path) -> bool:
        for allowed_root in self.extra_allowed_roots:
            try:
                resolved.relative_to(allowed_root)
                return True
            except ValueError:
                continue
        return False

    def _first_symlink_component(self, rel: str) -> str | None:
        current = self.worktree_root
        for part in PurePosixPath(rel).parts:
            current = current / part
            try:
                if current.is_symlink():
                    return current.relative_to(self.worktree_root).as_posix()
            except OSError:
                return None
        return None

    def _rel_path_in_scope(self, rel: str, *, tool_name: str) -> bool:
        rel_clean = _normalize_slashes(rel).rstrip("/")
        if not rel_clean:
            return False
        if tool_name == "fs_delete" and is_non_material_untracked_path(rel_clean):
            # Mirror the agent layer's carve-out: deleting non-material
            # untracked scratch output outside scope stays allowed.
            return True
        rel_cf = rel_clean.casefold()
        if tool_name == "fs_mkdir" and rel_cf in self._ancestor_dirs_cf:
            return True
        for pattern in self.allowed_patterns:
            if scope_path_matches_pattern(rel_clean, pattern, root=self.worktree_root):
                return True
        if not self.case_insensitive:
            return False
        for pattern in self.allowed_patterns:
            if self._matches_casefolded(rel_cf, pattern):
                return True
        return False

    def _matches_casefolded(self, rel_cf: str, pattern: str) -> bool:
        pattern_cf = _normalize_slashes(pattern).rstrip("/").casefold()
        if not pattern_cf:
            return False
        if any(ch in pattern_cf for ch in "*?["):
            variants = [pattern_cf]
            if "/**/" in pattern_cf:
                variants.append(pattern_cf.replace("/**/", "/"))
            return any(fnmatch.fnmatchcase(rel_cf, variant) for variant in variants)
        if rel_cf == pattern_cf:
            return True
        try:
            is_dir_scope = (self.worktree_root / pattern).is_dir()
        except OSError:
            is_dir_scope = False
        return is_dir_scope and rel_cf.startswith(pattern_cf + "/")
