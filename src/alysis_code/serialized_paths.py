from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

_ROOT_IDENTITY_FIELDS = frozenset({"workspace_root", "git_root"})
_PATH_FIELDS = frozenset(
    {
        "artifact_root",
        "binding_requested_path",
        "bundle_dir",
        "copied_log_path",
        "current_run_pointer_path",
        "cwd",
        "focus_dir",
        "focus_path",
        "log_path",
        "original_path",
        "path",
        "run_dir",
        "session_artifact_dir",
        "source_log_path",
        "zip_path",
    }
)
_COMMON_POSIX_ROOT_SEGMENTS = frozenset(
    {
        "Applications",
        "Library",
        "System",
        "Users",
        "Volumes",
        "bin",
        "boot",
        "data",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "nix",
        "opt",
        "private",
        "proc",
        "root",
        "run",
        "sbin",
        "snap",
        "srv",
        "sys",
        "tmp",
        "usr",
        "var",
    }
)
_TEXT_PATH_TERMINATORS = frozenset(" \t\r\n`\"'<>(),;![]{}")
_WINDOWS_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_UNC_ABS_RE = re.compile(r"^\\\\[^\\/\s]+[\\/][^\\/\s]+(?:[\\/].*)?$")
_GENERIC_POSIX_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9])(/(?!/)[^\s`\"'<>(),;!()[\]{}]+(?:/[^\s`\"'<>(),;!()[\]{}]+)*)"
)
_GENERIC_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]:[\\/][^\s`\"'<>(),;!()[\]{}\\\/]+(?:[\\/][^\s`\"'<>(),;!()[\]{}\\\/]+)*)"
)
_GENERIC_WINDOWS_UNC_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:\\\\)[^\s`\"'<>(),;!()[\]{}\\\/]+(?:[\\/][^\s`\"'<>(),;!()[\]{}\\\/]+)+)"
)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_key_name(value: str | None) -> str:
    return str(value or "").strip().lower()


def _coerce_path_value(value: str | Path | None) -> tuple[Path, str] | None:
    if value is None:
        return None
    if isinstance(value, Path):
        raw = str(value).strip()
        if not raw:
            return None
        return value, raw
    raw = str(value).strip()
    if not raw:
        return None
    return Path(raw), raw


def _is_posix_absolute_path_text(raw: str) -> bool:
    return raw.startswith("/") and not raw.startswith("//")


def _is_windows_drive_absolute_path_text(raw: str) -> bool:
    return bool(_WINDOWS_DRIVE_ABS_RE.match(raw))


def _is_windows_unc_absolute_path_text(raw: str) -> bool:
    return bool(_WINDOWS_UNC_ABS_RE.match(raw))


def _is_windows_absolute_path_text(raw: str) -> bool:
    return _is_windows_drive_absolute_path_text(raw) or _is_windows_unc_absolute_path_text(raw)


def _looks_like_absolute_path_text(raw: str) -> bool:
    return _is_posix_absolute_path_text(raw) or _is_windows_absolute_path_text(raw)


def _host_windows_root(root: Path | None) -> PureWindowsPath | None:
    if root is None:
        return None
    raw = str(root.resolve())
    if not _is_windows_absolute_path_text(raw):
        return None
    return PureWindowsPath(raw)


def _pure_path_is_relative_to(
    path: PurePosixPath | PureWindowsPath, root: PurePosixPath | PureWindowsPath
) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _serialized_path_basename(raw: str) -> str:
    if _is_windows_absolute_path_text(raw):
        basename = PureWindowsPath(raw).name
        if basename:
            return basename
    elif _is_posix_absolute_path_text(raw):
        basename = PurePosixPath(raw).name
        if basename:
            return basename

    parts = [part for part in re.split(r"[\\/]+", raw.rstrip("\\/")) if part]
    if not parts:
        return "path"
    return parts[-1].rstrip(":") or "path"


def redacted_host_path_label(value: str | Path | None) -> str | None:
    coerced = _coerce_path_value(value)
    if coerced is None:
        return None
    _, raw = coerced
    basename = _serialized_path_basename(raw)
    return f"[redacted host path: {basename}]"


def _workspace_root_candidates(workspace_root: Path) -> tuple[str, ...]:
    resolved = workspace_root.resolve()
    candidates = [str(resolved)]
    posix = resolved.as_posix()
    if posix not in candidates:
        candidates.append(posix)
    return tuple(sorted(candidates, key=len, reverse=True))


def _consume_path_suffix(text: str, *, start: int) -> tuple[str, int]:
    end = start
    while end < len(text) and text[end] not in _TEXT_PATH_TERMINATORS:
        end += 1
    return text[start:end], end


def _sanitize_workspace_root_paths_in_text(text: str, *, workspace_root: Path) -> str:
    sanitized = text
    for root_text in _workspace_root_candidates(workspace_root):
        pieces: list[str] = []
        cursor = 0
        while True:
            index = sanitized.find(root_text, cursor)
            if index < 0:
                pieces.append(sanitized[cursor:])
                break
            pieces.append(sanitized[cursor:index])
            suffix, end = _consume_path_suffix(sanitized, start=index + len(root_text))
            full_path = root_text + suffix
            pieces.append(
                safe_serialized_path(full_path, workspace_root=workspace_root) or full_path
            )
            cursor = end
        sanitized = "".join(pieces)
    return sanitized


def _posix_root_segment_exists(root_segment: str) -> bool:
    try:
        return Path("/" + root_segment).exists()
    except OSError:
        return False


def _looks_like_generic_posix_path(candidate: str) -> bool:
    coerced = _coerce_path_value(candidate)
    if coerced is None:
        return False
    _, raw = coerced
    if not _is_posix_absolute_path_text(raw):
        return False
    parts = PurePosixPath(raw).parts
    if len(parts) < 2:
        return False
    root_segment = str(parts[1] or "").strip()
    if not root_segment:
        return False
    # Free-form bundle sanitization must not treat arbitrary slash routes like
    # /v1/chat/completions as host paths. Only redact generic POSIX candidates
    # when they start from a known filesystem root or an actual root directory
    # on the current host.
    return root_segment in _COMMON_POSIX_ROOT_SEGMENTS or _posix_root_segment_exists(root_segment)


def sanitize_paths_in_text(text: str, *, workspace_root: Path | None = None) -> str:
    sanitized = str(text or "")
    if workspace_root is not None:
        # Replace known workspace-root prefixes first so repo-owned paths become
        # repo-relative even in plain-text artifacts like markdown summaries and logs.
        sanitized = _sanitize_workspace_root_paths_in_text(sanitized, workspace_root=workspace_root)

    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(1)
        if not _looks_like_generic_posix_path(candidate):
            return candidate
        return safe_serialized_path(candidate, workspace_root=workspace_root) or candidate

    def _replace_windows(match: re.Match[str]) -> str:
        candidate = match.group(1)
        if not _is_windows_absolute_path_text(candidate):
            return candidate
        return safe_serialized_path(candidate, workspace_root=workspace_root) or candidate

    sanitized = _GENERIC_POSIX_PATH_RE.sub(_replace, sanitized)
    sanitized = _GENERIC_WINDOWS_UNC_PATH_RE.sub(_replace_windows, sanitized)
    sanitized = _GENERIC_WINDOWS_PATH_RE.sub(_replace_windows, sanitized)
    return sanitized


def safe_serialized_path(
    value: str | Path | None,
    *,
    workspace_root: Path | None = None,
    bundle_root: Path | None = None,
    prefer_bundle_relative: bool = False,
) -> str | None:
    coerced = _coerce_path_value(value)
    if coerced is None:
        return None

    _, raw = coerced
    if not _looks_like_absolute_path_text(raw):
        return raw

    if _is_windows_absolute_path_text(raw):
        candidate = PureWindowsPath(raw)
        bundle_root_windows = _host_windows_root(bundle_root)
        workspace_root_windows = _host_windows_root(workspace_root)

        if (
            prefer_bundle_relative
            and bundle_root_windows is not None
            and _pure_path_is_relative_to(candidate, bundle_root_windows)
        ):
            rel = candidate.relative_to(bundle_root_windows).as_posix()
            return rel or "."

        if workspace_root_windows is not None and _pure_path_is_relative_to(
            candidate, workspace_root_windows
        ):
            rel = candidate.relative_to(workspace_root_windows).as_posix()
            return rel or "."

        return redacted_host_path_label(raw)

    # pathlib.Path absolute detection is host-dependent, so serialized Windows
    # strings are handled above with PureWindowsPath. Real host POSIX paths keep
    # the existing resolved/relative behavior here.
    resolved = Path(raw).expanduser().resolve(strict=False)
    bundle_root_resolved = bundle_root.resolve() if bundle_root is not None else None
    workspace_root_resolved = workspace_root.resolve() if workspace_root is not None else None

    if (
        prefer_bundle_relative
        and bundle_root_resolved is not None
        and _path_is_relative_to(resolved, bundle_root_resolved)
    ):
        rel = resolved.relative_to(bundle_root_resolved).as_posix()
        return rel or "."

    if workspace_root_resolved is not None and _path_is_relative_to(
        resolved, workspace_root_resolved
    ):
        rel = resolved.relative_to(workspace_root_resolved).as_posix()
        return rel or "."

    return redacted_host_path_label(raw)


def safe_serialized_path_field(
    field_name: str,
    value: str | Path | None,
    *,
    workspace_root: Path | None = None,
    bundle_root: Path | None = None,
    prefer_bundle_relative: bool = False,
) -> str | None:
    normalized = _normalize_key_name(field_name)
    if normalized == "workspace_root":
        if workspace_root is not None:
            coerced = _coerce_path_value(value)
            if coerced is not None:
                resolved = coerced[0].expanduser().resolve(strict=False)
                if resolved == workspace_root.resolve():
                    return "<workspace-root>"
        return safe_serialized_path(
            value,
            workspace_root=workspace_root,
            bundle_root=bundle_root,
            prefer_bundle_relative=prefer_bundle_relative,
        )

    if normalized == "git_root" and workspace_root is not None:
        coerced = _coerce_path_value(value)
        if coerced is not None:
            resolved = coerced[0].expanduser().resolve(strict=False)
            if resolved == workspace_root.resolve():
                return "<workspace-root>"

    return safe_serialized_path(
        value,
        workspace_root=workspace_root,
        bundle_root=bundle_root,
        prefer_bundle_relative=prefer_bundle_relative,
    )


def looks_like_serialized_path_field(field_name: str | None) -> bool:
    normalized = _normalize_key_name(field_name)
    if not normalized:
        return False
    if normalized in _ROOT_IDENTITY_FIELDS or normalized in _PATH_FIELDS:
        return True
    return normalized.endswith(("_path", "_dir", "_root"))
