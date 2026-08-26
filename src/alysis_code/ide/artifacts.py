from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .protocol import ProtocolError, redact_secrets

DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024
DEFAULT_MAX_ARTIFACTS = 500
MAX_ARTIFACTS = 5_000
DEFAULT_MAX_ARTIFACT_DEPTH = 8
MAX_ARTIFACT_DEPTH = 20


@dataclass(frozen=True, slots=True)
class ArtifactRoot:
    name: str
    path: Path


class ArtifactStore:
    def __init__(self, roots: list[ArtifactRoot]) -> None:
        self._roots = {root.name: root.path.resolve() for root in roots}

    def add_root(self, root: ArtifactRoot) -> None:
        self._roots[root.name] = root.path.resolve()

    def list(
        self,
        *,
        max_items: Any = DEFAULT_MAX_ARTIFACTS,
        max_depth: Any = DEFAULT_MAX_ARTIFACT_DEPTH,
    ) -> dict[str, Any]:
        max_items = _bounded_int(max_items, default=DEFAULT_MAX_ARTIFACTS, upper=MAX_ARTIFACTS)
        max_depth = _bounded_int(
            max_depth, default=DEFAULT_MAX_ARTIFACT_DEPTH, upper=MAX_ARTIFACT_DEPTH
        )
        artifacts: list[dict[str, Any]] = []
        truncated = False
        for root_name, root in sorted(self._roots.items()):
            if not root.exists() or not root.is_dir():
                continue
            stack: list[tuple[Path, int]] = [(root, 0)]
            while stack:
                current, depth = stack.pop()
                try:
                    entries = sorted(current.iterdir(), key=lambda path: path.name)
                except OSError:
                    continue
                for path in entries:
                    if path.is_symlink():
                        continue
                    if path.is_dir():
                        if depth < max_depth:
                            stack.append((path, depth + 1))
                        else:
                            truncated = True
                        continue
                    if not path.is_file():
                        continue
                    if len(artifacts) >= max_items:
                        truncated = True
                        return {
                            "artifacts": artifacts,
                            "truncated": truncated,
                            "max_items": max_items,
                            "max_depth": max_depth,
                        }
                    rel = path.relative_to(root).as_posix()
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    artifacts.append(
                        {
                            "artifact_id": f"{root_name}:{rel}",
                            "root": root_name,
                            "path": rel,
                            "size_bytes": size,
                        }
                    )
        return {
            "artifacts": artifacts,
            "truncated": truncated,
            "max_items": max_items,
            "max_depth": max_depth,
        }

    def read(
        self, artifact_id: str, *, max_bytes: Any = DEFAULT_MAX_ARTIFACT_BYTES
    ) -> dict[str, Any]:
        root_name, rel = _parse_artifact_id(artifact_id)
        root = self._roots.get(root_name)
        if root is None:
            raise ProtocolError("artifact_not_found", "Artifact root is not available.")
        max_bytes = _bounded_int(
            max_bytes, default=DEFAULT_MAX_ARTIFACT_BYTES, upper=MAX_ARTIFACT_BYTES
        )
        path = _resolve_safe(root, rel)
        if not path.exists() or not path.is_file():
            raise ProtocolError("artifact_not_found", "Artifact was not found.")
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                payload = handle.read(max_bytes + 1)
        except OSError as e:
            raise ProtocolError("artifact_not_found", "Artifact was not readable.") from e
        truncated = len(payload) > max_bytes or size > max_bytes
        payload = payload[:max_bytes]
        return {
            "artifact_id": artifact_id,
            "path": rel,
            "size_bytes": size,
            "truncated": truncated,
            "max_bytes": max_bytes,
            "encoding": "utf-8-replace",
            "content": redact_secrets(payload.decode("utf-8", errors="replace")),
        }


def _parse_artifact_id(artifact_id: str) -> tuple[str, str]:
    if ":" not in artifact_id:
        raise ProtocolError("invalid_artifact_id", "Artifact id must use '<root>:<path>'.")
    root, rel = artifact_id.split(":", 1)
    root = root.strip()
    rel = rel.strip()
    if not root or not rel:
        raise ProtocolError("invalid_artifact_id", "Artifact id root and path are required.")
    return root, rel


def _resolve_safe(root: Path, rel: str) -> Path:
    pure = PurePosixPath(rel.replace("\\", "/"))
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise ProtocolError("invalid_artifact_id", "Artifact path escapes its root.")
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise ProtocolError("invalid_artifact_id", "Artifact path escapes its root.") from e
    return candidate


def _bounded_int(value: Any, *, default: int, upper: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise ProtocolError("invalid_request", "Numeric artifact limits must be integers.") from e
    return max(1, min(parsed, upper))
