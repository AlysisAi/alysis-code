from __future__ import annotations

from typing import Any

from ..ide.protocol import redact_secrets
from ..session_artifacts import SessionArtifactLayout

DEFAULT_SESSION_ARTIFACT_READ_BYTES = 64 * 1024
MAX_SESSION_ARTIFACT_READ_BYTES = 1024 * 1024


class SessionArtifactReadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "session_artifact_read_failed",
        guidance: str | None = None,
    ) -> None:
        self.result_payload: dict[str, Any] = {
            "error": message,
            "error_code": error_code,
            "terminal": True,
            "retryable": False,
        }
        if guidance:
            self.result_payload["guidance"] = guidance
        super().__init__(message)


def _invalid_locator_error(exc: BaseException) -> SessionArtifactReadError:
    guidance = (
        "Use only a locator returned by a tool in this session. Guessing a locator will not work."
    )
    return SessionArtifactReadError(
        f"Invalid session artifact locator: {exc}. A valid locator starts with "
        "session_artifacts/ and uses relative POSIX path segments. "
        f"{guidance}",
        error_code="invalid_session_artifact_locator",
        guidance=guidance,
    )


def _bounded_max_bytes(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_SESSION_ARTIFACT_READ_BYTES
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SessionArtifactReadError("Session artifact max_bytes must be an integer.") from exc
    return max(1, min(parsed, MAX_SESSION_ARTIFACT_READ_BYTES))


def _bounded_offset(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SessionArtifactReadError("Session artifact offset must be an integer.") from exc
    if parsed < 0:
        raise SessionArtifactReadError("Session artifact offset must be non-negative.")
    return parsed


def session_artifact_read(
    *,
    artifact_layout: SessionArtifactLayout,
    locator: str,
    max_bytes: Any = DEFAULT_SESSION_ARTIFACT_READ_BYTES,
    offset: Any = 0,
) -> dict[str, Any]:
    """Read one bounded artifact addressed only by its current-session locator."""

    try:
        artifact_path = artifact_layout.resolve_locator(locator)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid_locator_error(exc) from exc

    read_limit = _bounded_max_bytes(max_bytes)
    read_offset = _bounded_offset(offset)
    if not artifact_path.exists() or not artifact_path.is_file():
        guidance = (
            "The session that produced the locator must read it. Do not retry this "
            "locator from the current session."
        )
        raise SessionArtifactReadError(
            "Session artifact is not present in this session and may belong to a "
            f"different session. {guidance}",
            error_code="session_artifact_session_mismatch",
            guidance=guidance,
        )
    try:
        size = artifact_path.stat().st_size
        if read_offset >= size:
            payload = b""
        else:
            with artifact_path.open("rb") as handle:
                handle.seek(read_offset)
                payload = handle.read(read_limit)
    except OSError as exc:
        raise SessionArtifactReadError("Session artifact was not readable.") from exc

    bytes_returned = len(payload)
    has_more = read_offset + bytes_returned < size
    content = payload.decode("utf-8", errors="replace")
    return {
        "locator": str(locator),
        "offset": read_offset,
        "bytes_returned": bytes_returned,
        "size": size,
        "size_bytes": size,
        "has_more": has_more,
        "next_offset": read_offset + bytes_returned if has_more else None,
        "truncated": has_more,
        "max_bytes": read_limit,
        "encoding": "utf-8-replace",
        "content": redact_secrets(content),
    }


__all__ = [
    "DEFAULT_SESSION_ARTIFACT_READ_BYTES",
    "MAX_SESSION_ARTIFACT_READ_BYTES",
    "SessionArtifactReadError",
    "session_artifact_read",
]
