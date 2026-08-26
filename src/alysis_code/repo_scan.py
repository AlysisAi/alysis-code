from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .file_classification import (
    BROAD_SOURCE_EXTENSIONS,
    CODE_SCAN_SKIP_DIR_NAMES,
    SOURCE_EXTENSIONS_BY_LANGUAGE,
    source_extensions_for_languages,
)
from .runtime_artifacts import RUNTIME_ARTIFACT_DIR_NAMES
from .verification_command_analysis import analyze_verification_command
from .workspace_context import WorkspaceContext
from .workspace_provisioning import detect_declared_test_runner, ini_has_section

REPO_SCAN_SCHEMA_VERSION = 1
_MAX_TOP_LEVEL_ENTRIES = 18
_MAX_README_EXCERPTS = 2
_MAX_EXCERPT_BYTES = 8192
_MAX_EXCERPT_CHARS = 1200
_MAX_EXCERPT_LINES = 24
_MAX_SUMMARY_FILES = 4
_MAX_SUMMARY_COMMANDS = 3
_MAX_MANIFEST_SCAN_DEPTH = 4
_MAX_MANIFEST_SCAN_DIRS = 160
_MAX_PYTHON_TEST_SIGNAL_DIRS = 64
_MAX_PYTHON_TEST_SIGNAL_FILES = 256
_MAX_REPRESENTATIVE_SOURCE_FILES = 12
_MAX_SOURCE_DISCOVERY_DIRS = 96
_MAX_SOURCE_DISCOVERY_DEPTH = 4
_SKIP_TOP_LEVEL_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
} | set(RUNTIME_ARTIFACT_DIR_NAMES)
_SKIP_RECURSIVE_NAMES = (
    _SKIP_TOP_LEVEL_NAMES
    | set(CODE_SCAN_SKIP_DIR_NAMES)
    | {
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "coverage",
        "vendor",
    }
)
_MANIFEST_SPECS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"),
    ("requirements.txt", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("pytest.ini", "python"),
    ("tox.ini", "python"),
    ("package.json", "node"),
    ("pnpm-lock.yaml", "node"),
    ("yarn.lock", "node"),
    ("package-lock.json", "node"),
    ("bun.lockb", "node"),
    ("tsconfig.json", "typescript"),
    ("pom.xml", "java"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("Makefile", "build"),
    ("justfile", "build"),
    ("Dockerfile", "docker"),
    ("docker-compose.yml", "docker"),
    ("docker-compose.yaml", "docker"),
    ("compose.yml", "docker"),
    ("compose.yaml", "docker"),
)
_RECURSIVE_MANIFEST_FILENAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "tsconfig.json",
    "pom.xml",
}
_README_NAMES = ("README.md", "README.rst", "README.txt", "README")
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*::?\s*$")
_SOURCE_EXTENSIONS_BY_LANGUAGE = SOURCE_EXTENSIONS_BY_LANGUAGE
_BROAD_SOURCE_EXTENSIONS = BROAD_SOURCE_EXTENSIONS


@dataclass(frozen=True)
class RepoScanResult:
    schema_version: int
    workspace_root: str
    focus_relpath: str
    workspace_kind: str
    git_root: str | None
    has_head_commit: bool
    current_branch: str | None
    top_level_entries: list[dict[str, str]]
    manifests: list[dict[str, str]]
    readme_paths: list[str]
    readme_excerpts: list[dict[str, str]]
    conventions_path: str | None
    conventions_excerpt: str | None
    language_hints: list[str]
    package_hints: list[str]
    likely_test_commands: list[str]
    observed_paths: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RepoScanResult:
        return cls(
            schema_version=int(raw.get("schema_version") or 0),
            workspace_root=str(raw.get("workspace_root") or ""),
            focus_relpath=str(raw.get("focus_relpath") or "."),
            workspace_kind=str(raw.get("workspace_kind") or ""),
            git_root=(None if raw.get("git_root") in (None, "") else str(raw.get("git_root"))),
            has_head_commit=bool(raw.get("has_head_commit", False)),
            current_branch=(
                None if raw.get("current_branch") in (None, "") else str(raw.get("current_branch"))
            ),
            top_level_entries=_list_of_dicts(raw.get("top_level_entries")),
            manifests=_list_of_dicts(raw.get("manifests")),
            readme_paths=_list_of_strings(raw.get("readme_paths")),
            readme_excerpts=_list_of_dicts(raw.get("readme_excerpts")),
            conventions_path=(
                None
                if raw.get("conventions_path") in (None, "")
                else str(raw.get("conventions_path"))
            ),
            conventions_excerpt=(
                None
                if raw.get("conventions_excerpt") in (None, "")
                else str(raw.get("conventions_excerpt"))
            ),
            language_hints=_list_of_strings(raw.get("language_hints")),
            package_hints=_list_of_strings(raw.get("package_hints")),
            likely_test_commands=_list_of_strings(raw.get("likely_test_commands")),
            observed_paths=_list_of_strings(raw.get("observed_paths")),
        )


def scan_workspace(*, context: WorkspaceContext) -> RepoScanResult:
    root = context.workspace_root
    search_dirs = _search_dirs(context)
    top_level_entries = _collect_top_level_entries(root)
    manifests = _collect_manifests(root=root, search_dirs=search_dirs)
    readme_paths = _collect_readmes(root=root, search_dirs=search_dirs)
    readme_excerpts = [
        {"path": rel_path, "excerpt": excerpt}
        for rel_path, excerpt in (
            _excerpt_entry(root=root, path=root / rel_path)
            for rel_path in readme_paths[:_MAX_README_EXCERPTS]
        )
        if excerpt
    ]
    conventions_path = _find_conventions_path(root=root, search_dirs=search_dirs)
    conventions_excerpt = None
    if conventions_path is not None:
        conventions_excerpt = _read_text_excerpt(root / conventions_path)
    language_hints = _infer_language_hints(manifests=manifests)
    representative_source_paths = _collect_representative_source_paths(
        root=root,
        search_dirs=search_dirs,
        language_hints=language_hints,
    )
    package_hints = _infer_package_hints(root=root, manifests=manifests)
    likely_test_commands = _infer_test_commands(
        root=root,
        search_dirs=search_dirs,
        manifests=manifests,
    )
    observed_paths = _observed_paths(
        top_level_entries=top_level_entries,
        manifests=manifests,
        readme_paths=readme_paths,
        conventions_path=conventions_path,
        representative_source_paths=representative_source_paths,
    )
    return RepoScanResult(
        schema_version=REPO_SCAN_SCHEMA_VERSION,
        workspace_root=os.fspath(root),
        focus_relpath=context.focus_relpath,
        workspace_kind=context.workspace_kind,
        git_root=(os.fspath(context.git_root) if context.git_root is not None else None),
        has_head_commit=context.has_head_commit,
        current_branch=context.current_branch,
        top_level_entries=top_level_entries,
        manifests=manifests,
        readme_paths=readme_paths,
        readme_excerpts=readme_excerpts,
        conventions_path=conventions_path,
        conventions_excerpt=conventions_excerpt,
        language_hints=language_hints,
        package_hints=package_hints,
        likely_test_commands=likely_test_commands,
        observed_paths=observed_paths,
    )


def render_repo_scan_markdown(scan: RepoScanResult) -> str:
    branch_label = scan.current_branch or ("(no HEAD)" if not scan.has_head_commit else "(unknown)")
    lines = [
        "# Workspace Summary",
        "",
        f"- Workspace Root: `{scan.workspace_root}`",
        f"- Focus Directory: `{scan.focus_relpath or '.'}`",
        f"- Workspace Kind: `{scan.workspace_kind}`",
        f"- Git Root: `{scan.git_root or '(none)'}`",
        f"- Current Branch: `{branch_label}`",
        "",
        "## Important Files",
        "",
    ]
    important_files: list[str] = []
    important_files.extend(item.get("path", "") for item in scan.manifests[:8])
    important_files.extend(scan.readme_paths[:4])
    if scan.conventions_path:
        important_files.append(scan.conventions_path)
    important_files.extend(_representative_observed_source_paths(scan.observed_paths)[:8])
    important_files = _unique_nonempty(important_files)
    if important_files:
        for rel_path in important_files:
            lines.append(f"- `{rel_path}`")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Language Hints", ""])
    if scan.language_hints:
        for hint in scan.language_hints:
            lines.append(f"- `{hint}`")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Likely Verification Commands", ""])
    if scan.likely_test_commands:
        for command in scan.likely_test_commands:
            lines.append(f"- `{command}`")
    else:
        lines.append("- (none)")

    if scan.readme_excerpts:
        lines.extend(["", "## README Excerpts", ""])
        for item in scan.readme_excerpts:
            rel_path = str(item.get("path") or "").strip() or "(unknown)"
            excerpt = str(item.get("excerpt") or "").strip()
            lines.append(f"### `{rel_path}`")
            lines.append("")
            lines.append(excerpt or "(empty)")
            lines.append("")

    if scan.conventions_path:
        lines.extend(["## CONVENTIONS.md Excerpt", ""])
        lines.append(f"`{scan.conventions_path}`")
        lines.append("")
        lines.append(scan.conventions_excerpt or "(empty)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_repo_scan_summary_lines(scan: RepoScanResult) -> list[str]:
    branch_detail = (
        scan.current_branch
        if scan.current_branch
        else ("no HEAD" if not scan.has_head_commit else "unknown branch")
    )
    lines = [
        f"Workspace root: {scan.workspace_root}",
        f"Focus directory: {scan.focus_relpath or '.'}",
        f"Workspace kind: {scan.workspace_kind} ({branch_detail})",
    ]
    signals = _unique_nonempty(
        [item.get("path", "") for item in scan.manifests[:_MAX_SUMMARY_FILES]]
        + scan.readme_paths[:_MAX_SUMMARY_FILES]
        + ([scan.conventions_path] if scan.conventions_path else [])
    )
    if signals:
        lines.append(f"Important files: {', '.join(signals[:_MAX_SUMMARY_FILES])}")
    if scan.likely_test_commands:
        lines.append(
            "Likely verify: " + ", ".join(scan.likely_test_commands[:_MAX_SUMMARY_COMMANDS])
        )
    return lines


def _list_of_strings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _list_of_dicts(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append({str(key): str(value) for key, value in item.items() if value is not None})
    return out


def _search_dirs(context: WorkspaceContext) -> list[Path]:
    dirs = [context.workspace_root]
    if context.focus_path != context.workspace_root:
        dirs.append(context.focus_path)
    return dirs


def _collect_top_level_entries(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if candidate.name in _SKIP_TOP_LEVEL_NAMES:
            continue
        kind = "dir" if candidate.is_dir() else ("file" if candidate.is_file() else "other")
        entries.append({"path": candidate.name, "kind": kind})
        if len(entries) >= _MAX_TOP_LEVEL_ENTRIES:
            break
    return entries


def _source_extensions_for_languages(language_hints: list[str]) -> set[str]:
    return source_extensions_for_languages(language_hints)


def _collect_representative_source_paths(
    *,
    root: Path,
    search_dirs: list[Path],
    language_hints: list[str],
) -> list[str]:
    extensions = _source_extensions_for_languages(language_hints)
    seen: set[str] = set()
    out: list[str] = []
    seen_bases: set[Path] = set()
    for base_dir in search_dirs:
        try:
            resolved_base = base_dir.resolve()
        except OSError:
            continue
        if resolved_base in seen_bases:
            continue
        seen_bases.add(resolved_base)
        visited_dirs = 0
        try:
            walker = os.walk(base_dir, topdown=True)
        except OSError:
            continue
        for current_dir, dirnames, filenames in walker:
            visited_dirs += 1
            if visited_dirs > _MAX_SOURCE_DISCOVERY_DIRS:
                dirnames[:] = []
                break
            current_path = Path(current_dir)
            try:
                rel_to_base = current_path.resolve().relative_to(resolved_base)
            except (OSError, ValueError):
                dirnames[:] = []
                continue
            depth = 0 if rel_to_base == Path(".") else len(rel_to_base.parts)
            dirnames[:] = sorted(
                (
                    name
                    for name in dirnames
                    if name not in _SKIP_RECURSIVE_NAMES and not name.startswith(".")
                ),
                key=str.casefold,
            )
            if depth >= _MAX_SOURCE_DISCOVERY_DEPTH:
                dirnames[:] = []
            for filename in sorted(filenames, key=str.casefold):
                if filename.startswith("."):
                    continue
                candidate = current_path / filename
                if candidate.suffix.casefold() not in extensions:
                    continue
                try:
                    rel_path = _relpath(root, candidate)
                except (OSError, ValueError):
                    continue
                if rel_path in seen:
                    continue
                seen.add(rel_path)
                out.append(rel_path)
                if len(out) >= _MAX_REPRESENTATIVE_SOURCE_FILES:
                    return out
    return out


def _collect_manifests(*, root: Path, search_dirs: list[Path]) -> list[dict[str, str]]:
    manifests: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in search_dirs:
        for filename, kind in _MANIFEST_SPECS:
            candidate = directory / filename
            if not candidate.exists() or not candidate.is_file():
                continue
            rel_path = _relpath(root, candidate)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            manifests.append({"path": rel_path, "kind": kind})
        for rel_path, kind in _walk_nested_manifests(root=root, base_dir=directory):
            if rel_path in seen:
                continue
            seen.add(rel_path)
            manifests.append({"path": rel_path, "kind": kind})
    manifests.sort(key=lambda item: item["path"].casefold())
    return manifests


def _walk_nested_manifests(*, root: Path, base_dir: Path) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    visited_dirs = 0
    manifest_kind_map = dict(_MANIFEST_SPECS)
    try:
        walker = os.walk(base_dir, topdown=True)
    except OSError:
        return results

    for current_dir, dirnames, filenames in walker:
        current_path = Path(current_dir)
        try:
            rel_to_base = current_path.resolve().relative_to(base_dir.resolve())
        except ValueError:
            dirnames[:] = []
            continue
        depth = 0 if rel_to_base == Path(".") else len(rel_to_base.parts)
        dirnames[:] = sorted(
            (
                name
                for name in dirnames
                if name not in _SKIP_RECURSIVE_NAMES and not name.startswith(".git")
            ),
            key=str.casefold,
        )
        if depth >= _MAX_MANIFEST_SCAN_DEPTH:
            dirnames[:] = []
        visited_dirs += 1
        if visited_dirs > _MAX_MANIFEST_SCAN_DIRS:
            break
        for filename in sorted(filenames, key=str.casefold):
            if filename not in _RECURSIVE_MANIFEST_FILENAMES:
                continue
            candidate = current_path / filename
            if not candidate.is_file():
                continue
            kind = manifest_kind_map.get(filename)
            if not kind:
                continue
            results.append((_relpath(root, candidate), kind))
    return results


def _collect_readmes(*, root: Path, search_dirs: list[Path]) -> list[str]:
    readmes: list[str] = []
    seen: set[str] = set()
    for directory in search_dirs:
        for candidate in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
            if not candidate.is_file():
                continue
            if candidate.name.casefold() not in {name.casefold() for name in _README_NAMES}:
                continue
            rel_path = _relpath(root, candidate)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            readmes.append(rel_path)
    return readmes


def _find_conventions_path(*, root: Path, search_dirs: list[Path]) -> str | None:
    for directory in reversed(search_dirs):
        candidate = directory / "CONVENTIONS.md"
        if candidate.exists() and candidate.is_file():
            return _relpath(root, candidate)
    return None


def _excerpt_entry(*, root: Path, path: Path) -> tuple[str, str]:
    return _relpath(root, path), _read_text_excerpt(path)


def _read_text_excerpt(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_EXCERPT_BYTES)
    except OSError:
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    excerpt = "\n".join(lines[:_MAX_EXCERPT_LINES]).strip()
    if len(excerpt) > _MAX_EXCERPT_CHARS:
        excerpt = excerpt[:_MAX_EXCERPT_CHARS].rstrip() + "..."
    return excerpt


def _infer_language_hints(*, manifests: list[dict[str, str]]) -> list[str]:
    kinds = {str(item.get("kind") or "") for item in manifests}
    hints: list[str] = []
    if "python" in kinds:
        hints.append("python")
    if "node" in kinds:
        hints.append("javascript")
    if any(
        item.get("path") == "tsconfig.json" or item.get("path", "").endswith("/tsconfig.json")
        for item in manifests
    ):
        hints.append("typescript")
    if "go" in kinds:
        hints.append("go")
    if "rust" in kinds:
        hints.append("rust")
    if "java" in kinds:
        hints.append("java")
    if "docker" in kinds:
        hints.append("docker")
    return hints


def _infer_package_hints(*, root: Path, manifests: list[dict[str, str]]) -> list[str]:
    manifest_paths = {item.get("path", ""): item.get("kind", "") for item in manifests}
    hints: list[str] = []
    pyproject_path = _find_manifest_path(manifests, "pyproject.toml")
    if pyproject_path is not None:
        tool_names = _pyproject_tool_names(root / pyproject_path)
        if "poetry" in tool_names:
            hints.append("poetry")
        if "uv" in tool_names:
            hints.append("uv")
        if "hatch" in tool_names:
            hints.append("hatch")
        if "setuptools" in tool_names:
            hints.append("setuptools")
        if not tool_names:
            hints.append("python")
    elif any(kind == "python" for kind in manifest_paths.values()):
        hints.append("python")

    package_json_path = _find_manifest_path(manifests, "package.json")
    if package_json_path is not None:
        hints.append(
            _node_package_manager(
                root=root, package_json_path=package_json_path, manifests=manifests
            )
        )
    elif any(kind == "node" for kind in manifest_paths.values()):
        hints.append("node")

    if any(kind == "go" for kind in manifest_paths.values()):
        hints.append("go-mod")
    if any(kind == "rust" for kind in manifest_paths.values()):
        hints.append("cargo")
    if any(kind == "java" for kind in manifest_paths.values()):
        hints.append("maven")
    if _find_manifest_path(manifests, "Makefile") is not None:
        hints.append("make")
    if _find_manifest_path(manifests, "justfile") is not None:
        hints.append("just")
    if any(kind == "docker" for kind in manifest_paths.values()):
        hints.append("docker")
    return _unique_nonempty(hints)


def _infer_test_commands(
    *,
    root: Path,
    search_dirs: list[Path],
    manifests: list[dict[str, str]],
) -> list[str]:
    commands: list[str] = []

    makefile_path = _find_manifest_path(manifests, "Makefile")
    if makefile_path is not None:
        target = _preferred_make_target(root / makefile_path)
        if target is not None:
            commands.append(f"make {target}")

    justfile_path = _find_manifest_path(manifests, "justfile")
    if justfile_path is not None:
        target = _preferred_make_target(root / justfile_path)
        if target is not None:
            commands.append(f"just {target}")

    for package_json_path in _prefer_leaf_service_manifests(
        _find_manifest_paths(manifests, "package.json")
    ):
        package_path = root / package_json_path
        if _package_json_has_real_test_script(package_path):
            commands.append(
                _node_test_command(
                    root=root,
                    package_json_path=package_json_path,
                    manifests=manifests,
                )
            )
        for script_name in _package_json_safe_verification_scripts(package_path):
            commands.append(
                _node_script_command(
                    root=root,
                    package_json_path=package_json_path,
                    manifests=manifests,
                    script_name=script_name,
                )
            )

    for pom_path in _prefer_leaf_service_manifests(_find_manifest_paths(manifests, "pom.xml")):
        commands.append(_maven_test_command(root=root, pom_path=pom_path))

    if _find_manifest_path(manifests, "go.mod") is not None:
        commands.append("go test ./...")

    if _find_manifest_path(manifests, "Cargo.toml") is not None:
        commands.append("cargo test")

    python_test_command = _python_test_command(
        root=root,
        search_dirs=search_dirs,
        manifests=manifests,
    )
    if python_test_command is not None:
        commands.append(python_test_command)
    commands.extend(_python_configured_check_commands(root=root, manifests=manifests))

    # Doctest over README/docs files is deliberately never inferred: it asserts
    # that the prose examples still render, not that the code works, so it is
    # never an authoritative verification surface for a change to the code.

    return _unique_nonempty(commands)


def _preferred_make_target(path: Path) -> str | None:
    excerpt = _read_text_excerpt(path)
    if not excerpt:
        return None
    targets: list[str] = []
    for line in excerpt.splitlines():
        if line.startswith("\t") or line.startswith(" "):
            continue
        match = _MAKE_TARGET_RE.match(line.strip())
        if match:
            targets.append(match.group(1))
    for candidate in ("test", "check", "verify"):
        if candidate in targets:
            return candidate
    return None


def _likely_has_python_tests(
    *,
    root: Path,
    search_dirs: list[Path],
    manifests: list[dict[str, str]],
) -> bool:
    return _python_test_command(root=root, search_dirs=search_dirs, manifests=manifests) is not None


def _python_test_command(
    *,
    root: Path,
    search_dirs: list[Path],
    manifests: list[dict[str, str]],
) -> str | None:
    declared_runner = detect_declared_test_runner(root)
    for directory in _python_test_search_dirs(root=root, search_dirs=search_dirs):
        if _directory_has_python_test_layout_signal(directory, allow_tests_dir_hint=False):
            rel_path = _relpath(root, directory)
            if declared_runner is None and _directory_declares_unittest_suite(directory):
                if rel_path in {"", "."}:
                    return "python -m unittest discover"
                return f"python -m unittest discover -s {shlex.quote(rel_path)}"
            if rel_path in {"", "."}:
                return "pytest -q"
            quoted = shlex.quote(rel_path)
            return f"PYTHONPATH={quoted} pytest -q {quoted}"
    return None


def _python_configured_check_commands(
    *,
    root: Path,
    manifests: list[dict[str, str]],
) -> list[str]:
    declared: list[str] = []
    pyproject_path = _find_manifest_path(manifests, "pyproject.toml")
    if pyproject_path is not None:
        tool_names = _pyproject_tool_names(root / pyproject_path)
        if "ruff" in tool_names:
            declared.append("ruff check .")
        if "mypy" in tool_names:
            declared.append("mypy .")

    setup_cfg_path = _find_manifest_path(manifests, "setup.cfg")
    if setup_cfg_path is not None:
        config_text = _read_bounded_config(root / setup_cfg_path)
        if config_text is not None and ini_has_section(config_text, "ruff"):
            declared.append("ruff check .")
        if config_text is not None and ini_has_section(config_text, "mypy"):
            declared.append("mypy .")

    safe: list[str] = []
    for command in _unique_nonempty(declared):
        analysis = analyze_verification_command(
            command,
            trusted=True,
            workspace_root=root,
        )
        if analysis.is_valid_verifier:
            safe.append(command)
    return safe


def _read_bounded_config(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_EXCERPT_BYTES)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def _directory_declares_unittest_suite(directory: Path) -> bool:
    seen_dirs = 0
    seen_files = 0
    saw_unittest = False
    try:
        for current_root, dirnames, filenames in os.walk(directory):
            seen_dirs += 1
            if seen_dirs > _MAX_PYTHON_TEST_SIGNAL_DIRS:
                return False
            dirnames[:] = sorted(
                [name for name in dirnames if name not in _SKIP_RECURSIVE_NAMES],
                key=str.casefold,
            )
            for filename in sorted(filenames, key=str.casefold):
                if not _is_unittest_discovery_signal_file(filename):
                    continue
                seen_files += 1
                if seen_files > _MAX_PYTHON_TEST_SIGNAL_FILES:
                    return False
                text = _read_bounded_config(Path(current_root) / filename)
                if text is None:
                    continue
                if re.search(r"(?m)^(?:async\s+)?def\s+test_", text) or re.search(
                    r"(?m)^\s*(?:from\s+pytest\s+import\b|import\s+pytest\b)",
                    text,
                ):
                    return False
                if re.search(
                    r"(?m)^\s*(?:from\s+unittest\s+import\b|import\s+unittest\b)",
                    text,
                ):
                    saw_unittest = True
    except OSError:
        return False
    return saw_unittest


def detect_fallback_test_commands(*, root: Path) -> list[str]:
    """Last-resort runner detection for a workspace with no usable command.

    Deliberately *not* part of the primary inference above, which stays
    conservative on purpose: a repo that declares pytest but ships no tests
    should not advertise a verification contract it cannot honour. This chain is
    consulted only once the selected command has turned out to be unusable, when
    the alternative is running with no verification surface at all -- there, a
    runner the repo declares (or one ``unittest`` can discover) is strictly
    better than nothing, and a runner that collects zero tests is reported as
    skipped rather than passed.
    """
    workspace_root = Path(root)
    if detect_declared_test_runner(workspace_root) is not None:
        return ["pytest -q"]
    if _directory_has_python_test_layout_signal(workspace_root, allow_tests_dir_hint=False):
        return ["pytest -q"]
    if _directory_has_unittest_discovery_signal(workspace_root):
        return ["python -m unittest discover"]
    return []


def _python_test_search_dirs(*, root: Path, search_dirs: list[Path]) -> list[Path]:
    focused: list[Path] = []
    workspace_roots: list[Path] = []
    for directory in search_dirs:
        rel_path = _relpath(root, directory)
        if rel_path in {"", "."}:
            workspace_roots.append(directory)
        else:
            focused.append(directory)
    return [*focused, *workspace_roots]


def _directory_has_python_test_layout_signal(
    directory: Path,
    *,
    allow_tests_dir_hint: bool,
) -> bool:
    try:
        for candidate in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
            if candidate.is_file():
                if _is_python_test_signal_file(candidate.name):
                    return True
                continue
            if not candidate.is_dir() or candidate.name != "tests":
                continue
            if allow_tests_dir_hint:
                return True
            if _tests_dir_has_python_signal(candidate):
                return True
    except OSError:
        return False
    return False


def _is_python_test_signal_file(name: str) -> bool:
    lowered = str(name or "").strip().casefold()
    if lowered == "conftest.py":
        return True
    return lowered.endswith(".py") and (lowered.startswith("test_") or lowered.endswith("_test.py"))


def _is_unittest_discovery_signal_file(name: str) -> bool:
    """Mirror ``unittest discover``'s own default pattern, ``test*.py``.

    Deliberately the runner's rule rather than a hand-written guess at what a
    test module looks like: the point of this layer is "would ``unittest``
    collect anything here", and only ``unittest``'s pattern answers that. A
    helper module that happens to match collects zero tests, which the verifier
    already reports as skipped rather than passed.
    """
    lowered = str(name or "").strip().casefold()
    return lowered.startswith("test") and lowered.endswith(".py")


def _directory_has_unittest_discovery_signal(directory: Path) -> bool:
    try:
        for candidate in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
            if candidate.is_file():
                if _is_unittest_discovery_signal_file(candidate.name):
                    return True
                continue
            if not candidate.is_dir() or candidate.name != "tests":
                continue
            if _tests_dir_has_python_signal(candidate, match=_is_unittest_discovery_signal_file):
                return True
    except OSError:
        return False
    return False


def _tests_dir_has_python_signal(
    tests_dir: Path,
    *,
    match: Callable[[str], bool] = _is_python_test_signal_file,
) -> bool:
    seen_dirs = 0
    seen_files = 0
    try:
        for current_root, dirnames, filenames in os.walk(tests_dir):
            seen_dirs += 1
            if seen_dirs > _MAX_PYTHON_TEST_SIGNAL_DIRS:
                break
            dirnames[:] = sorted(
                [name for name in dirnames if name not in _SKIP_RECURSIVE_NAMES],
                key=str.casefold,
            )
            for filename in sorted(filenames, key=str.casefold):
                seen_files += 1
                if seen_files > _MAX_PYTHON_TEST_SIGNAL_FILES:
                    return False
                if match(filename):
                    return True
            _ = current_root
    except OSError:
        return False
    return False


def _package_json_has_real_test_script(path: Path) -> bool:
    payload = _read_json_object(path)
    if payload is None:
        return False
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return False
    raw = scripts.get("test")
    if not isinstance(raw, str):
        return False
    script = raw.strip()
    if not script:
        return False
    lowered = script.casefold()
    if "no test specified" in lowered:
        return False
    return True


def _package_json_safe_verification_scripts(path: Path) -> list[str]:
    payload = _read_json_object(path)
    scripts = payload.get("scripts") if payload is not None else None
    if not isinstance(scripts, dict):
        return []
    candidate_names = [name for name in ("check", "lint", "typecheck") if name in scripts]
    candidate_names.extend(
        sorted(
            (str(name) for name in scripts if isinstance(name, str) and name.startswith("test:")),
            key=str.casefold,
        )
    )
    safe_names: list[str] = []
    for name in candidate_names:
        script = scripts.get(name)
        if not isinstance(script, str) or not script.strip():
            continue
        analysis = analyze_verification_command(
            script,
            trusted=True,
            workspace_root=path.parent,
        )
        if analysis.is_valid_verifier:
            safe_names.append(name)
    return safe_names


def _node_package_manager(
    *,
    root: Path,
    package_json_path: str,
    manifests: list[dict[str, str]],
) -> str:
    payload = _read_json_object(root / package_json_path)
    if payload is not None:
        raw_manager = payload.get("packageManager")
        if isinstance(raw_manager, str):
            lowered = raw_manager.strip().casefold()
            if lowered.startswith("pnpm@"):
                return "pnpm"
            if lowered.startswith("yarn@"):
                return "yarn"
            if lowered.startswith("bun@"):
                return "bun"
            if lowered.startswith("npm@"):
                return "npm"
    manifest_paths = {item.get("path", "") for item in manifests}
    package_dir = PurePosixPath(package_json_path).parent
    search_dirs = [package_dir, *package_dir.parents]
    for candidate_dir in search_dirs:
        prefix = "" if str(candidate_dir) in {"", "."} else f"{candidate_dir.as_posix()}/"
        if f"{prefix}pnpm-lock.yaml" in manifest_paths:
            return "pnpm"
        if f"{prefix}yarn.lock" in manifest_paths:
            return "yarn"
        if f"{prefix}bun.lockb" in manifest_paths:
            return "bun"
        if f"{prefix}package-lock.json" in manifest_paths:
            return "npm"
    return "npm"


def _node_test_command(
    *,
    root: Path,
    package_json_path: str,
    manifests: list[dict[str, str]],
) -> str:
    manager = _node_package_manager(
        root=root, package_json_path=package_json_path, manifests=manifests
    )
    package_dir = PurePosixPath(package_json_path).parent.as_posix()
    if package_dir in {"", "."}:
        return f"{manager} test"
    quoted_dir = shlex.quote(package_dir)
    if manager == "pnpm":
        return f"pnpm --dir {quoted_dir} test"
    if manager == "yarn":
        return f"yarn --cwd {quoted_dir} test"
    if manager == "bun":
        return f"bun --cwd {quoted_dir} test"
    return f"npm --prefix {quoted_dir} test"


def _node_script_command(
    *,
    root: Path,
    package_json_path: str,
    manifests: list[dict[str, str]],
    script_name: str,
) -> str:
    manager = _node_package_manager(
        root=root,
        package_json_path=package_json_path,
        manifests=manifests,
    )
    quoted_script = shlex.quote(script_name)
    package_dir = PurePosixPath(package_json_path).parent.as_posix()
    if package_dir in {"", "."}:
        return f"{manager} run {quoted_script}"
    quoted_dir = shlex.quote(package_dir)
    if manager == "pnpm":
        return f"pnpm --dir {quoted_dir} run {quoted_script}"
    if manager == "yarn":
        return f"yarn --cwd {quoted_dir} run {quoted_script}"
    if manager == "bun":
        return f"bun --cwd {quoted_dir} run {quoted_script}"
    return f"npm --prefix {quoted_dir} run {quoted_script}"


def _maven_test_command(*, root: Path, pom_path: str) -> str:
    wrapper = _find_maven_wrapper(root=root, pom_path=pom_path)
    if pom_path == "pom.xml":
        return f"{wrapper} test" if wrapper else "mvn test"
    quoted_pom = shlex.quote(pom_path)
    return f"{wrapper} -f {quoted_pom} test" if wrapper else f"mvn -f {quoted_pom} test"


def _find_maven_wrapper(*, root: Path, pom_path: str) -> str | None:
    pom_dir = PurePosixPath(pom_path).parent
    candidate_paths: list[PurePosixPath] = []
    if str(pom_dir) not in {"", "."}:
        candidate_paths.append(pom_dir / "mvnw")
    candidate_paths.append(PurePosixPath("mvnw"))
    for rel_path in candidate_paths:
        candidate = root / rel_path.as_posix()
        if candidate.exists() and candidate.is_file():
            rendered = rel_path.as_posix()
            return rendered if rendered.startswith("./") else f"./{rendered}"
    return None


def _pyproject_tool_names(path: Path) -> set[str]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return set()

    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_EXCERPT_BYTES)
    except OSError:
        return set()
    try:
        payload = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return set()
    tool = payload.get("tool")
    if not isinstance(tool, dict):
        return set()
    return {str(key).strip().casefold() for key in tool if str(key).strip()}


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_EXCERPT_BYTES)
    except OSError:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _find_manifest_path(manifests: list[dict[str, str]], filename: str) -> str | None:
    suffix = f"/{filename}"
    for item in manifests:
        rel_path = str(item.get("path") or "")
        if rel_path == filename or rel_path.endswith(suffix):
            return rel_path
    return None


def _find_manifest_paths(manifests: list[dict[str, str]], filename: str) -> list[str]:
    suffix = f"/{filename}"
    matches: list[str] = []
    for item in manifests:
        rel_path = str(item.get("path") or "")
        if rel_path == filename or rel_path.endswith(suffix):
            matches.append(rel_path)
    return matches


def _prefer_leaf_service_manifests(paths: list[str]) -> list[str]:
    if not paths:
        return []
    nested = [path for path in paths if "/" in path]
    return nested if nested else paths


def _observed_paths(
    *,
    top_level_entries: list[dict[str, str]],
    manifests: list[dict[str, str]],
    readme_paths: list[str],
    conventions_path: str | None,
    representative_source_paths: list[str],
) -> list[str]:
    observed = _unique_nonempty(
        [item.get("path", "") for item in top_level_entries]
        + [item.get("path", "") for item in manifests]
        + readme_paths
        + ([conventions_path] if conventions_path else [])
        + representative_source_paths
    )
    return observed


def _representative_observed_source_paths(paths: list[str]) -> list[str]:
    return [
        path for path in paths if PurePosixPath(path).suffix.casefold() in _BROAD_SOURCE_EXTENSIONS
    ]


def _relpath(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _unique_nonempty(values: list[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
