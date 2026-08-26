from __future__ import annotations

import shlex


def _unquote_path(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if raw[0] == raw[-1] == '"' and len(raw) >= 2:
        try:
            parsed = shlex.split(raw)
            if parsed:
                return parsed[0]
        except ValueError:
            return raw[1:-1]
        return raw[1:-1]
    return raw


def _normalize_git_path(value: str) -> str:
    raw = _unquote_path(value.strip())
    if raw.startswith("a/") or raw.startswith("b/"):
        return raw[2:]
    return raw


def parse_diff_git_line(line: str) -> tuple[str | None, str | None]:
    if not line.startswith("diff --git "):
        return None, None
    payload = line[len("diff --git ") :].strip()

    if payload.startswith("a/"):
        split = payload.rsplit(" b/", 1)
        if len(split) == 2:
            a_raw = split[0]
            b_raw = "b/" + split[1]
            return _normalize_git_path(a_raw), _normalize_git_path(b_raw)

    try:
        parts = shlex.split(payload)
    except ValueError:
        return None, None
    if len(parts) < 2:
        return None, None
    return _normalize_git_path(parts[0]), _normalize_git_path(parts[1])


def _parse_patch_header_path(line: str, prefix: str) -> str | None:
    if not line.startswith(prefix):
        return None
    raw = line[len(prefix) :].split("\t", 1)[0].strip()
    if not raw:
        return None
    path = _normalize_git_path(raw)
    if not path or path == "/dev/null":
        return None
    return path


def iter_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    saw_diff_header = False

    for line in patch_text.splitlines():
        a_path, b_path = parse_diff_git_line(line)
        if a_path is None and b_path is None:
            continue
        saw_diff_header = True
        for path in (a_path, b_path):
            if not path or path == "/dev/null" or path in seen:
                continue
            seen.add(path)
            paths.append(path)

    if saw_diff_header:
        return paths

    for line in patch_text.splitlines():
        for prefix in ("--- ", "+++ "):
            path = _parse_patch_header_path(line, prefix)
            if path is None or path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return paths


def parse_patch_changed_files(patch_text: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    saw_diff_header = False

    for line in patch_text.splitlines():
        a_path, b_path = parse_diff_git_line(line)
        if a_path is None and b_path is None:
            continue
        saw_diff_header = True
        if b_path and b_path != "/dev/null" and b_path not in seen:
            seen.add(b_path)
            files.append(b_path)

    if saw_diff_header:
        return files

    for line in patch_text.splitlines():
        path = _parse_patch_header_path(line, "+++ ")
        if path is None or path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files
