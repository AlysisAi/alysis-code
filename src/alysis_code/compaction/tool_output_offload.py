from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ide.protocol import redact_secrets
from ..session_artifacts import SessionArtifactLayout
from ..tools.registry import summarize_tool_output_chunk


@dataclass(frozen=True)
class OffloadResult:
    content_for_message: str
    offloaded: bool
    transcript_shaped: bool
    artifact_locator: str | None
    artifact_fs_path: str | None
    artifact_readable_via_fs: bool
    artifact_location: str | None
    original_chars: int
    preview_chars: int
    message_chars: int
    error: str | None = None


def _safe_component(raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw.strip())
    return clean or "x"


def _truncated_preview(content: str, limit: int) -> tuple[str, int]:
    if limit <= 0:
        return ("", 0)
    if len(content) <= limit:
        return (content, len(content))
    return (content[:limit] + "...(truncated)", limit)


def _path_is_under_root(*, path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
    except OSError:
        return True


def _has_safe_directory_ancestors(*, path: Path, root: Path) -> bool:
    """Require every existing descendant of root to be a real contained directory."""

    root_abs = root.resolve()
    try:
        relative = path.absolute().relative_to(root_abs)
    except (OSError, RuntimeError, ValueError):
        return False
    current = root_abs
    for component in relative.parts:
        current = current / component
        try:
            exists = current.exists()
        except OSError:
            return False
        if not exists:
            continue
        if _is_link_like(current) or not current.is_dir():
            return False
        try:
            current.resolve().relative_to(root_abs)
        except (OSError, RuntimeError, ValueError):
            return False
    return True


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # Windows ACLs are not represented completely by POSIX mode bits.
        pass


def _atomic_private_write_text(
    path: Path,
    content: str,
    *,
    containment_root: Path | None = None,
) -> None:
    """Publish one complete private artifact or leave the old path untouched."""

    if containment_root is not None and not _has_safe_directory_ancestors(
        path=path.parent,
        root=containment_root,
    ):
        raise OSError("tool output artifact directory is not safely contained")
    _ensure_private_directory(path.parent)
    if containment_root is not None and not _has_safe_directory_ancestors(
        path=path.parent,
        root=containment_root,
    ):
        raise OSError("tool output artifact directory is not safely contained")
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        # A directory fsync makes the rename durable on filesystems that support it.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


class ToolOutputOffloader:
    def __init__(
        self,
        *,
        artifact_layout: SessionArtifactLayout,
        workspace_root: Path | None,
        threshold_chars: int,
        preview_chars: int,
        workspace_artifacts_enabled: bool = True,
    ) -> None:
        self._artifact_layout = artifact_layout
        self._session_artifact_root = artifact_layout.filesystem_root.resolve()
        self._workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self._threshold_chars = max(1, int(threshold_chars))
        self._preview_chars = max(1, int(preview_chars))
        # Retained for constructor compatibility; artifacts always follow the
        # session store, including when that store is outside the workspace.
        _ = workspace_artifacts_enabled

    def _transcript_shape_threshold_chars(self) -> int:
        return self._threshold_chars

    def _storage_layout(self) -> SessionArtifactLayout:
        return self._artifact_layout

    @property
    def artifact_root(self) -> Path:
        """Session-scoped root containing offloaded outputs."""

        return self._storage_layout().filesystem_root.resolve()

    @staticmethod
    def _full_output_guidance(
        *,
        original_chars: int,
        preview_chars: int,
        artifact_locator: str | None,
        fs_read_path: str | None,
        artifact_readable_via_fs: bool,
        artifact_location: str | None,
    ) -> str:
        readable_path = str(fs_read_path or artifact_locator or "").strip()
        if artifact_locator:
            return (
                f"Truncated to {preview_chars} of {original_chars} chars. "
                f"Full output saved as {artifact_locator}; use session_artifact_read "
                "with that locator."
            )
        if artifact_readable_via_fs and readable_path:
            return (
                f"Truncated to {preview_chars} of {original_chars} chars. "
                f"Full output saved to {artifact_location or 'workspace_root'} at "
                f"{readable_path} - use fs_read on that path to view all of it."
            )
        return (
            f"Truncated to {preview_chars} of {original_chars} chars; the rest is not "
            "readable via fs. Re-run the command narrowed (grep/head/sed/awk) to get "
            "the specific slice you need."
        )

    def _build_transcript_stub(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        step: int,
        content_json: str,
        original_chars: int,
        preview_text: str,
        preview_chars: int,
        offloaded: bool,
        transcript_shaped: bool,
        artifact_locator: str | None = None,
        artifact_saved: bool = False,
        artifact_readable_via_fs: bool = False,
        artifact_location: str | None = None,
        fs_read_path: str | None = None,
        error: str | None = None,
        continuation_metadata: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "step": step,
            "offloaded": bool(offloaded),
            "summary": str(redact_secrets(summarize_tool_output_chunk(tool_name, content_json))),
            "preview": str(redact_secrets(preview_text)),
            "preview_chars": preview_chars,
            "original_chars": original_chars,
            "content_truncated": original_chars > preview_chars,
            "raw_saved_in_session_log": False,
        }
        if transcript_shaped and not offloaded:
            payload["transcript_shaped"] = True
        if continuation_metadata:
            payload.update(continuation_metadata)
        if offloaded:
            payload["artifact_locator"] = artifact_locator
            payload["artifact_saved"] = artifact_saved
            payload["artifact_readable_via_fs"] = artifact_readable_via_fs
            payload["artifact_location"] = artifact_location
            if artifact_readable_via_fs and fs_read_path:
                payload["fs_read_path"] = fs_read_path
        if original_chars > preview_chars:
            payload["full_output"] = self._full_output_guidance(
                original_chars=original_chars,
                preview_chars=preview_chars,
                artifact_locator=artifact_locator,
                fs_read_path=fs_read_path,
                artifact_readable_via_fs=artifact_readable_via_fs,
                artifact_location=artifact_location,
            )
        if error:
            payload["error"] = str(redact_secrets(error))
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    def _artifact_path(self, *, tool_name: str, tool_call_id: str, step: int) -> Path:
        safe_tool = _safe_component(tool_name)
        safe_call_id = _safe_component(tool_call_id)
        filename = f"step{max(0, int(step))}_{safe_tool}_{safe_call_id}.json"
        storage_layout = self._storage_layout()
        storage_root = storage_layout.filesystem_root.resolve()
        candidate = storage_layout.artifact_fs_path("tool_outputs", filename)
        candidate.parent.resolve().relative_to(storage_root)
        if candidate.is_symlink():
            raise ValueError("tool output artifact path cannot be a symlink")
        return candidate

    def maybe_offload(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        step: int,
        result: Any,
        content_json: str,
    ) -> OffloadResult:
        original_chars = len(content_json)
        continuation_metadata = None
        if isinstance(result, dict) and result.get("truncated") is True:
            continuation_metadata = {
                key: result.get(key)
                for key in ("total_lines", "returned_range", "next_range")
                if key in result
            }
        preview_text, preview_chars = _truncated_preview(content_json, self._preview_chars)
        shape_threshold_chars = self._transcript_shape_threshold_chars()
        if original_chars < shape_threshold_chars:
            return OffloadResult(
                content_for_message=content_json,
                offloaded=False,
                transcript_shaped=False,
                artifact_locator=None,
                artifact_fs_path=None,
                artifact_readable_via_fs=False,
                artifact_location=None,
                original_chars=original_chars,
                preview_chars=preview_chars,
                message_chars=original_chars,
                error=None,
            )

        if original_chars < self._threshold_chars:
            content_for_message = self._build_transcript_stub(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
                content_json=content_json,
                original_chars=original_chars,
                preview_text=preview_text,
                preview_chars=preview_chars,
                offloaded=False,
                transcript_shaped=True,
                continuation_metadata=continuation_metadata,
            )
            return OffloadResult(
                content_for_message=content_for_message,
                offloaded=False,
                transcript_shaped=True,
                artifact_locator=None,
                artifact_fs_path=None,
                artifact_readable_via_fs=False,
                artifact_location=None,
                original_chars=original_chars,
                preview_chars=preview_chars,
                message_chars=len(content_for_message),
                error=None,
            )

        try:
            artifact_abs = self._artifact_path(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
            )
            artifact_abs.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "step": step,
                "content_json": content_json,
            }
            _atomic_private_write_text(
                artifact_abs,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                containment_root=(
                    self._workspace_root
                    if _path_is_under_root(path=artifact_abs, root=self._workspace_root)
                    else None
                ),
            )
            storage_layout = self._storage_layout()
            artifact_ref = storage_layout.model_reference_for_path(
                artifact_path=artifact_abs,
                workspace_root=self._workspace_root,
            )
            fs_read_path = None
            if artifact_ref.artifact_readable_via_fs and self._workspace_root is not None:
                fs_read_path = (
                    artifact_abs.resolve().relative_to(self._workspace_root.resolve()).as_posix()
                )
            content_for_message = self._build_transcript_stub(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
                content_json=content_json,
                original_chars=original_chars,
                preview_text=preview_text,
                preview_chars=preview_chars,
                offloaded=True,
                transcript_shaped=True,
                artifact_locator=artifact_ref.locator,
                artifact_saved=True,
                artifact_readable_via_fs=artifact_ref.artifact_readable_via_fs,
                artifact_location=artifact_ref.artifact_location,
                fs_read_path=fs_read_path,
                continuation_metadata=continuation_metadata,
            )
            return OffloadResult(
                content_for_message=content_for_message,
                offloaded=True,
                transcript_shaped=True,
                artifact_locator=artifact_ref.locator,
                artifact_fs_path=str(artifact_abs),
                artifact_readable_via_fs=artifact_ref.artifact_readable_via_fs,
                artifact_location=artifact_ref.artifact_location,
                original_chars=original_chars,
                preview_chars=preview_chars,
                message_chars=len(content_for_message),
                error=None,
            )
        except Exception as exc:
            safe_error = str(redact_secrets(str(exc)))
            fallback_preview, fallback_preview_chars = _truncated_preview(
                content_json,
                self._preview_chars,
            )
            content_for_message = self._build_transcript_stub(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step=step,
                content_json=content_json,
                original_chars=original_chars,
                preview_text=fallback_preview,
                preview_chars=fallback_preview_chars,
                offloaded=False,
                transcript_shaped=True,
                error=safe_error,
                continuation_metadata=continuation_metadata,
            )
            return OffloadResult(
                content_for_message=content_for_message,
                offloaded=False,
                transcript_shaped=True,
                artifact_locator=None,
                artifact_fs_path=None,
                artifact_readable_via_fs=False,
                artifact_location=None,
                original_chars=original_chars,
                preview_chars=fallback_preview_chars,
                message_chars=len(content_for_message),
                error=safe_error,
            )
