from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .settings import ServerSettings


class ServerStoreError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _make_id(prefix: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{ts}_{uuid4().hex[:8]}"


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _sanitize_zip_parts(name: str) -> list[str]:
    clean_name = name.replace("\\", "/")
    pure = PurePosixPath(clean_name)
    parts = [part for part in pure.parts if part not in {"", "."}]
    if pure.is_absolute():
        raise ServerStoreError(f"Zip entry uses absolute path: {name!r}")
    if any(part == ".." for part in parts):
        raise ServerStoreError(f"Zip entry escapes workspace: {name!r}")
    return parts


def _resolve_within(base: Path, rel_parts: list[str]) -> Path:
    target = (base / Path(*rel_parts)).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError as e:
        raise ServerStoreError(f"Zip entry escapes workspace: {'/'.join(rel_parts)!r}") from e
    return target


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    run_dir: Path
    workspace_dir: Path
    jobs_dir: Path


@dataclass(frozen=True)
class JobPaths:
    job_id: str
    job_dir: Path
    logs_path: Path
    meta_path: Path
    result_path: Path


class ServerStore:
    def __init__(self, settings: ServerSettings) -> None:
        self._settings = settings
        self._runs_dir = settings.data_dir / "runs"
        self._runs_dir.mkdir(parents=True, exist_ok=True)

    def create_empty_run(self) -> str:
        run_id = _make_id("run")
        run_paths = self._init_run_paths(run_id)
        self._write_run_meta(run_paths, source="empty")
        return run_id

    def create_run_from_zip_path(self, upload_path: Path) -> str:
        try:
            upload_size = upload_path.stat().st_size
        except OSError as e:
            raise ServerStoreError("Failed to read uploaded ZIP.") from e

        if upload_size > self._settings.max_upload_bytes:
            raise ServerStoreError(
                f"Upload too large ({upload_size} bytes); "
                f"max is {self._settings.max_upload_bytes} bytes."
            )
        run_id = _make_id("run")
        run_paths = self._init_run_paths(run_id)
        self._extract_zip_to_workspace(run_paths.workspace_dir, upload_path)
        self._write_run_meta(run_paths, source="zip")
        return run_id

    def get_run_paths(self, run_id: str) -> RunPaths:
        run_dir = self._runs_dir / run_id
        workspace_dir = run_dir / "workspace"
        jobs_dir = run_dir / "jobs"
        if not run_dir.exists() or not workspace_dir.exists() or not jobs_dir.exists():
            raise ServerStoreError(f"Run not found: {run_id}")
        return RunPaths(
            run_id=run_id,
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            jobs_dir=jobs_dir,
        )

    def create_job_paths(self, run_id: str, job_id: str) -> JobPaths:
        run_paths = self.get_run_paths(run_id)
        job_dir = run_paths.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        return JobPaths(
            job_id=job_id,
            job_dir=job_dir,
            logs_path=job_dir / "logs.txt",
            meta_path=job_dir / "meta.json",
            result_path=job_dir / "result.json",
        )

    def _init_run_paths(self, run_id: str) -> RunPaths:
        run_dir = self._runs_dir / run_id
        if run_dir.exists():
            raise ServerStoreError(f"Run already exists: {run_id}")
        workspace_dir = run_dir / "workspace"
        jobs_dir = run_dir / "jobs"
        workspace_dir.mkdir(parents=True, exist_ok=False)
        jobs_dir.mkdir(parents=True, exist_ok=False)
        return RunPaths(
            run_id=run_id,
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            jobs_dir=jobs_dir,
        )

    def _write_run_meta(self, run_paths: RunPaths, *, source: str) -> None:
        payload = {
            "run_id": run_paths.run_id,
            "created_at": _now_iso(),
            "source": source,
            "workspace": os.fspath(run_paths.workspace_dir),
        }
        (run_paths.run_dir / "run.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _extract_zip_to_workspace(self, workspace_dir: Path, upload_path: Path) -> None:
        try:
            zf = zipfile.ZipFile(upload_path)
        except zipfile.BadZipFile as e:
            raise ServerStoreError("Invalid ZIP file.") from e

        with zf:
            file_infos: list[tuple[zipfile.ZipInfo, list[str]]] = []
            total_uncompressed = 0
            uncompressed_limit = self._settings.max_upload_bytes * 4
            first_parts: set[str] = set()

            for info in zf.infolist():
                if info.is_dir():
                    continue
                if _is_zip_symlink(info):
                    continue
                parts = _sanitize_zip_parts(info.filename)
                if not parts:
                    continue
                total_uncompressed += max(0, int(info.file_size))
                if total_uncompressed > uncompressed_limit:
                    raise ServerStoreError(
                        f"ZIP exceeds uncompressed size limit ({uncompressed_limit} bytes)."
                    )
                first_parts.add(parts[0])
                file_infos.append((info, parts))

            strip_single_root = len(first_parts) == 1 and all(
                len(parts) > 1 for _, parts in file_infos
            )

            for info, parts in file_infos:
                rel_parts = parts[1:] if strip_single_root else parts
                if not rel_parts:
                    continue
                target = _resolve_within(workspace_dir, rel_parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
