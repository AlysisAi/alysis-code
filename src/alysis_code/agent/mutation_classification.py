from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from ..runtime_artifacts import is_runtime_artifact_path


class MutationPathCategory(StrEnum):
    BENIGN_RUNTIME_ARTIFACT = "BENIGN_RUNTIME_ARTIFACT"
    MATERIAL_SOURCE_OR_CONFIG = "MATERIAL_SOURCE_OR_CONFIG"
    MATERIAL_DELIVERABLE = "MATERIAL_DELIVERABLE"
    UNKNOWN_MATERIAL = "UNKNOWN_MATERIAL"


_SOURCE_DIR_NAMES = {
    "app",
    "apps",
    "bin",
    "cmd",
    "lib",
    "pkg",
    "packages",
    "server",
    "src",
    "test",
    "tests",
}
_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
_CONFIG_SUFFIXES = {".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml"}
_CONFIG_FILENAMES = {
    ".env",
    "cargo.lock",
    "cargo.toml",
    "dockerfile",
    "go.mod",
    "go.sum",
    "justfile",
    "makefile",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tsconfig.json",
    "yarn.lock",
}


@dataclass(frozen=True)
class MutationPathClassification:
    path: str
    category: MutationPathCategory
    reason: str
    tracked: bool | None = None
    existed_before: bool | None = None

    @property
    def is_material(self) -> bool:
        return self.category != MutationPathCategory.BENIGN_RUNTIME_ARTIFACT

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "category": self.category.value,
            "reason": self.reason,
            "tracked": self.tracked,
            "existed_before": self.existed_before,
        }


def _normalize_rel_path(path: str) -> str:
    cleaned = str(path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _requested_paths_contain(path: str, requested_paths: set[str] | None) -> bool:
    if not requested_paths:
        return False
    key = path.casefold()
    return any(key == _normalize_rel_path(item).casefold() for item in requested_paths)


def _looks_like_source_or_config_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = tuple(part.casefold() for part in pure.parts if part not in {"", "."})
    if not parts:
        return False
    name = pure.name.casefold()
    suffix = pure.suffix.casefold()
    if name in _CONFIG_FILENAMES:
        return True
    if suffix in _CONFIG_SUFFIXES:
        return True
    if suffix in _SOURCE_SUFFIXES:
        return True
    return bool(set(parts[:-1]) & _SOURCE_DIR_NAMES)


def classify_mutation_path(
    path: str,
    *,
    root: Path | None = None,
    requested_paths: set[str] | None = None,
    tracked: bool | None = None,
    existed_before: bool | None = None,
    command_was_verification: bool = False,
) -> MutationPathClassification:
    normalized = _normalize_rel_path(path)
    if not normalized:
        return MutationPathClassification(
            path="",
            category=MutationPathCategory.UNKNOWN_MATERIAL,
            reason="empty_path",
            tracked=tracked,
            existed_before=existed_before,
        )
    if is_runtime_artifact_path(normalized, root=root):
        return MutationPathClassification(
            path=normalized,
            category=MutationPathCategory.BENIGN_RUNTIME_ARTIFACT,
            reason="runtime_artifact_policy",
            tracked=tracked,
            existed_before=existed_before,
        )
    if _requested_paths_contain(normalized, requested_paths) or existed_before is False:
        return MutationPathClassification(
            path=normalized,
            category=MutationPathCategory.MATERIAL_DELIVERABLE,
            reason="requested_or_new_output",
            tracked=tracked,
            existed_before=existed_before,
        )
    if _looks_like_source_or_config_path(normalized):
        return MutationPathClassification(
            path=normalized,
            category=MutationPathCategory.MATERIAL_SOURCE_OR_CONFIG,
            reason="source_or_config_path",
            tracked=tracked,
            existed_before=existed_before,
        )
    return MutationPathClassification(
        path=normalized,
        category=MutationPathCategory.UNKNOWN_MATERIAL,
        reason="conservative_material_fallback",
        tracked=tracked,
        existed_before=existed_before,
    )


def classify_mutation_paths(
    paths: set[str] | list[str] | tuple[str, ...],
    *,
    root: Path | None = None,
    requested_paths: set[str] | None = None,
    command_was_verification: bool = False,
) -> tuple[MutationPathClassification, ...]:
    return tuple(
        classify_mutation_path(
            path,
            root=root,
            requested_paths=requested_paths,
            command_was_verification=command_was_verification,
        )
        for path in sorted({str(item) for item in paths if str(item).strip()})
    )


def material_mutation_paths(
    paths: set[str] | list[str] | tuple[str, ...],
    *,
    root: Path | None = None,
    requested_paths: set[str] | None = None,
    command_was_verification: bool = False,
) -> list[str]:
    return [
        item.path
        for item in classify_mutation_paths(
            paths,
            root=root,
            requested_paths=requested_paths,
            command_was_verification=command_was_verification,
        )
        if item.is_material
    ]


def benign_runtime_mutation_paths(
    paths: set[str] | list[str] | tuple[str, ...],
    *,
    root: Path | None = None,
) -> list[str]:
    return [
        item.path
        for item in classify_mutation_paths(paths, root=root)
        if item.category == MutationPathCategory.BENIGN_RUNTIME_ARTIFACT
    ]
