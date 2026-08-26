from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PYTEST_FAILED_RE = re.compile(
    r"\bFAILED\s+(?P<nodeid>(?P<path>(?:[A-Za-z]:)?[^\s:]+\.py)(?:::[^\s]+)+)(?:\s+-\s+(?P<msg>.+))?"
)
_PYTHON_FILE_RE = re.compile(
    r'File "(?P<path>[^"]+\.py)", line (?P<line>\d+), in (?P<symbol>[^\s]+)'
)
_PYTEST_SHORT_FILE_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.py):(?P<line>\d+):(?:\s+)?(?P<msg>.+)"
)
_JS_FAIL_RE = re.compile(r"\b(?:FAIL|Failed)\s+(?P<path>[^\s]+(?:\.[cm]?[jt]sx?))")
_JS_FRAME_RE = re.compile(
    r"(?:at\s+(?P<symbol>[^\s(]+)\s+\()?(?P<path>[A-Za-z0-9_./\\-]+(?:\.[cm]?[jt]sx?)):(?P<line>\d+):(?P<column>\d+)\)?"
)
_GO_FAIL_RE = re.compile(r"---\s+FAIL:\s+(?P<name>[A-Za-z0-9_/.-]+)")
_GO_FILE_RE = re.compile(r"(?P<path>[A-Za-z0-9_./\\-]+\.go):(?P<line>\d+):\s*(?P<msg>.+)")
_RUST_FAIL_RE = re.compile(r"----\s+(?P<name>[^-\n]+?)\s+stdout\s+----")
_RUST_PANIC_RE = re.compile(
    r"panicked at (?P<path>[A-Za-z0-9_./\\-]+\.rs):(?P<line>\d+):(?P<column>\d+)"
)
_JAVA_FRAME_RE = re.compile(
    r"at\s+(?P<symbol>[A-Za-z0-9_.$<>]+)\((?P<path>[A-Za-z0-9_.$/-]+\.java):(?P<line>\d+)\)"
)
_PATH_LINE_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|go|rs|java)):(?P<line>\d+)"
)
_EXCEPTION_SUMMARY_RE = re.compile(
    r"^(?:[A-Za-z_][\w.]*\.)?[A-Za-z_]\w*(?:Error|Exception|Warning|Interrupt|Exit):(?:\s+.+)?$"
)

_ERROR_MARKERS = (
    "AssertionError",
    "ModuleNotFoundError",
    "ImportError",
    "TypeError",
    "ValueError",
    "RuntimeError",
    "SyntaxError",
    "Error:",
    "Exception:",
    "panic",
    "panicked at",
    "FAILED ",
    "--- FAIL:",
)
_SKIP_PATH_PARTS = {
    ".git",
    ".venv",
    "venv",
    "site-packages",
    "node_modules",
    "__pycache__",
    "dist-packages",
}


def summarize_verification_failure(
    *,
    root: Path,
    command: str,
    effective_command: str,
    output: str,
    output_truncated: bool = False,
) -> dict[str, Any] | None:
    text = str(output or "")
    if not text.strip():
        return None

    framework = _infer_framework(command=effective_command or command, output=text)
    failing_tests = _extract_failing_tests(root=root, output=text, framework=framework)
    stack_frames = _extract_stack_frames(root=root, output=text)
    primary_error = _primary_error_line(text)
    likely_files = _dedupe(
        [
            *[item.get("path", "") for item in failing_tests],
            *[item.get("path", "") for item in stack_frames],
        ]
    )[:10]

    if not primary_error and not failing_tests and not stack_frames:
        return None

    confidence = 0.45
    if failing_tests:
        confidence = 0.8
    elif stack_frames:
        confidence = 0.65
    elif primary_error:
        confidence = 0.55

    return {
        "framework": framework,
        "primary_error": primary_error,
        "failing_tests": failing_tests[:8],
        "stack_frames": stack_frames[:12],
        "likely_next_files": likely_files,
        "output_truncated": bool(output_truncated),
        "heuristic": True,
        "confidence": confidence,
    }


def _infer_framework(*, command: str, output: str) -> str:
    combined = f"{command}\n{output}".casefold()
    if "pytest" in combined or "::test" in combined:
        return "pytest"
    if "vitest" in combined:
        return "vitest"
    if "jest" in combined or re.search(r"\bFAIL\s+.*\.(?:js|jsx|ts|tsx)\b", output):
        return "jest"
    if "go test" in combined or "--- fail:" in combined:
        return "go test"
    if "cargo test" in combined or "panicked at" in combined:
        return "cargo test"
    if "mvn test" in combined or "gradle" in combined or ".java:" in combined:
        return "junit"
    if "traceback (most recent call last)" in combined:
        return "python"
    return "unknown"


def _extract_failing_tests(*, root: Path, output: str, framework: str) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    for match in _PYTEST_FAILED_RE.finditer(output):
        path = _normalize_path(root=root, raw_path=match.group("path"))
        if not path:
            continue
        nodeid = _normalize_pytest_nodeid(normalized_path=path, raw_nodeid=match.group("nodeid"))
        item: dict[str, Any] = {
            "id": nodeid,
            "path": path,
        }
        msg = (match.group("msg") or "").strip()
        if msg:
            item["message"] = _clip(msg, 240)
        tests.append(item)

    if framework in {"go test", "unknown"}:
        current_name = ""
        for line in output.splitlines():
            name_match = _GO_FAIL_RE.search(line)
            if name_match:
                current_name = name_match.group("name")
                continue
            file_match = _GO_FILE_RE.search(line)
            if file_match and current_name:
                path = _normalize_path(root=root, raw_path=file_match.group("path"))
                if not path:
                    continue
                tests.append(
                    {
                        "id": current_name,
                        "path": path,
                        "line": int(file_match.group("line")),
                        "message": _clip(file_match.group("msg").strip(), 240),
                    }
                )

    if framework in {"cargo test", "unknown"}:
        for match in _RUST_FAIL_RE.finditer(output):
            name = " ".join(match.group("name").split())
            if name:
                tests.append({"id": name})

    return _dedupe_dicts(tests)


def _extract_stack_frames(*, root: Path, output: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for regex in (_PYTHON_FILE_RE, _JS_FRAME_RE, _GO_FILE_RE, _RUST_PANIC_RE, _JAVA_FRAME_RE):
        for match in regex.finditer(output):
            path = _normalize_path(root=root, raw_path=match.group("path"))
            if not path:
                continue
            frame: dict[str, Any] = {
                "path": path,
                "line": int(match.group("line")),
            }
            symbol = match.groupdict().get("symbol")
            if symbol:
                frame["symbol"] = symbol
            frames.append(frame)

    for match in _PATH_LINE_RE.finditer(output):
        path = _normalize_path(root=root, raw_path=match.group("path"))
        if not path:
            continue
        frames.append({"path": path, "line": int(match.group("line"))})

    return _dedupe_dicts(frames)


def _primary_error_line(output: str) -> str:
    lines = [" ".join(line.strip().split()) for line in output.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("E "):
            return _clip(line[2:].strip(), 320)
    for line in reversed(lines):
        if _EXCEPTION_SUMMARY_RE.match(line):
            return _clip(line, 320)
    for marker in _ERROR_MARKERS:
        for line in lines:
            if marker == "FAILED " and " - " in line:
                continue
            if line.startswith(("raise ", "assert ")):
                continue
            if marker in line:
                return _clip(line, 320)
    for line in lines:
        lowered = line.casefold()
        if "error" in lowered or "failed" in lowered or "exception" in lowered:
            return _clip(line, 320)
    return ""


def _normalize_path(*, root: Path, raw_path: str) -> str:
    clean = str(raw_path or "").strip().strip("'\"")
    if not clean:
        return ""
    clean = clean.replace("\\", "/")
    parts = {part for part in clean.split("/") if part}
    if parts & _SKIP_PATH_PARTS:
        return ""

    path_obj = Path(clean)
    root_abs = root.resolve()
    try:
        resolved = path_obj.resolve() if path_obj.is_absolute() else (root_abs / path_obj).resolve()
        relative = resolved.relative_to(root_abs).as_posix()
    except (OSError, ValueError):
        return ""
    return "" if relative == "." else relative


def _normalize_pytest_nodeid(*, normalized_path: str, raw_nodeid: str) -> str:
    _path_part, sep, suffix = str(raw_nodeid or "").strip().partition("::")
    return f"{normalized_path}{sep}{suffix}" if sep else normalized_path


def _clip(text: str, limit: int) -> str:
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = tuple(sorted((str(k), str(v)) for k, v in item.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
