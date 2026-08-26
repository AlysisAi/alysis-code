from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "alysis_code"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
LOCAL_TOP_LEVEL = "alysis_code"
DEPENDENCY_GROUPS = ("dependencies",)
OPTIONAL_DEPENDENCY_GROUPS = ("server",)

IMPORT_TO_DISTRIBUTION = {
    "PIL": "pillow",
    "multipart": "python-multipart",
    "prompt_toolkit": "prompt-toolkit",
}


@dataclass(frozen=True)
class ImportUse:
    module: str
    path: Path
    line: int


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if not match:
        raise ValueError(f"Invalid dependency entry in pyproject.toml: {requirement!r}")
    return _normalize_distribution_name(match.group(1))


def _declared_dependencies(pyproject_path: Path) -> set[str]:
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Cannot find {pyproject_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Cannot parse {pyproject_path}: {exc}") from exc

    project = data.get("project")
    if not isinstance(project, dict):
        raise RuntimeError(f"{pyproject_path} is missing a [project] table")

    declared: set[str] = set()
    for group in DEPENDENCY_GROUPS:
        dependencies = project.get(group, [])
        if not isinstance(dependencies, list):
            raise RuntimeError(f"[project].{group} must be a list")
        declared.update(_dependency_name(str(dependency)) for dependency in dependencies)

    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise RuntimeError("[project.optional-dependencies] must be a table when present")
    for group in OPTIONAL_DEPENDENCY_GROUPS:
        dependencies = optional.get(group, [])
        if not isinstance(dependencies, list):
            raise RuntimeError(f"[project.optional-dependencies].{group} must be a list")
        declared.update(_dependency_name(str(dependency)) for dependency in dependencies)

    return declared


def _is_stdlib(module: str) -> bool:
    root = module.split(".", 1)[0]
    return root in sys.stdlib_module_names


def _is_local(module: str) -> bool:
    root = module.split(".", 1)[0]
    return root == LOCAL_TOP_LEVEL


def _iter_python_files(source_root: Path) -> list[Path]:
    if not source_root.exists():
        raise RuntimeError(f"Cannot find source package at {source_root}")
    return sorted(path for path in source_root.rglob("*.py") if path.is_file())


def _imports_from_file(path: Path) -> list[ImportUse]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(f"Cannot parse {path}: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc

    imports: list[ImportUse] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(ImportUse(alias.name, path, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0 or not node.module:
                continue
            imports.append(ImportUse(node.module, path, node.lineno))
    return imports


def _distribution_for_import(module: str) -> str:
    root = module.split(".", 1)[0]
    return _normalize_distribution_name(IMPORT_TO_DISTRIBUTION.get(root, root))


def find_missing_dependencies() -> list[tuple[str, Path, int]]:
    declared = _declared_dependencies(PYPROJECT)
    missing: list[tuple[str, Path, int]] = []
    seen: set[tuple[str, Path, int]] = set()

    for path in _iter_python_files(SOURCE_ROOT):
        for import_use in _imports_from_file(path):
            module = import_use.module
            if _is_stdlib(module) or _is_local(module):
                continue
            distribution = _distribution_for_import(module)
            if distribution in declared:
                continue
            entry = (distribution, import_use.path, import_use.line)
            if entry not in seen:
                seen.add(entry)
                missing.append(entry)

    return missing


def main() -> int:
    try:
        missing = find_missing_dependencies()
    except RuntimeError as exc:
        print(f"Dependency audit failed: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Dependency audit failed: {exc}", file=sys.stderr)
        return 2

    if missing:
        for distribution, path, line in missing:
            relative_path = path.relative_to(PROJECT_ROOT).as_posix()
            print(f"{distribution} imported in {relative_path}:{line}")
        return 1

    print("No missing dependencies found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
