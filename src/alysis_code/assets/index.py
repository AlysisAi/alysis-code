from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from ..atomic_io import atomic_write_json
from ..forge import RunPaths, now_iso
from .models import (
    AssetAlreadyExistsError,
    AssetError,
    AssetNotFoundError,
    AssetRecord,
    ComprehensionRecord,
)
from .paths import (
    asset_comprehension_current_path,
    asset_comprehension_version_path,
)

ASSET_INDEX_SCHEMA_VERSION = 2
_LOCK_POLL_SECONDS = 0.02
_LOCK_TIMEOUT_SECONDS = 5.0
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()


def _process_lock_for_path(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


class AssetIndex:
    def __init__(self, run_paths: RunPaths) -> None:
        self.run_paths = run_paths

    def load(self) -> dict[str, Any]:
        path = self.run_paths.assets_index_path
        try:
            raw_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty_payload()
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise AssetError(f"Invalid asset index JSON: {path}") from e
        if not isinstance(payload, dict):
            raise AssetError(f"Invalid asset index structure: {path}")
        schema_version = int(payload.get("schema_version") or 0)
        if schema_version != ASSET_INDEX_SCHEMA_VERSION:
            raise AssetError(
                "Unsupported asset index schema_version "
                f"{schema_version}; run the legacy asset migration before loading this index."
            )
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise AssetError("Invalid asset index: assets must be an array")
        by_sha256 = payload.get("by_sha256")
        if not isinstance(by_sha256, dict):
            raise AssetError("Invalid asset index: by_sha256 must be an object")
        return payload

    def records(self, *, include_deleted: bool = False) -> list[AssetRecord]:
        records = [AssetRecord.from_dict(item) for item in self.load().get("assets", [])]
        if include_deleted:
            return records
        return [record for record in records if record.deleted_at is None]

    def get(self, asset_id: str, *, include_deleted: bool = False) -> AssetRecord:
        for record in self.records(include_deleted=include_deleted):
            if record.id == asset_id:
                return record
        raise AssetNotFoundError(f"Asset not found: {asset_id}")

    def find_by_sha256(self, sha256: str, *, include_deleted: bool = False) -> AssetRecord | None:
        digest = sha256.strip().lower()
        for record in self.records(include_deleted=include_deleted):
            if record.sha256 == digest:
                return record
        return None

    def add(
        self,
        record: AssetRecord,
        *,
        dedupe_policy: Literal["reject", "link"] = "reject",
    ) -> AssetRecord:
        with _AssetIndexLock(self.run_paths.assets_index_lock_path):
            payload = self.load()
            existing = self._find_active_by_sha(payload, record.sha256)
            if existing is not None:
                if dedupe_policy == "link":
                    return existing
                raise AssetAlreadyExistsError(existing.id)
            records = [AssetRecord.from_dict(item) for item in payload.get("assets", [])]
            if any(item.id == record.id for item in records):
                raise AssetError(f"Asset id collision: {record.id}")
            records.append(record)
            self._write_records(records)
            return record

    def add_with_publish(
        self,
        *,
        sha256: str,
        dedupe_policy: Literal["reject", "link"],
        build_record: Callable[[set[str]], AssetRecord],
        publish: Callable[[AssetRecord], None],
        rollback: Callable[[AssetRecord], None] | None = None,
    ) -> AssetRecord:
        with _AssetIndexLock(self.run_paths.assets_index_lock_path):
            payload = self.load()
            existing = self._find_active_by_sha(payload, sha256)
            if existing is not None:
                if dedupe_policy == "link":
                    return existing
                raise AssetAlreadyExistsError(existing.id)
            records = [AssetRecord.from_dict(item) for item in payload.get("assets", [])]
            record = build_record({item.id for item in records})
            if any(item.id == record.id for item in records):
                raise AssetError(f"Asset id collision: {record.id}")
            try:
                publish(record)
                records.append(record)
                self._write_records(records)
            except Exception:
                if rollback is not None:
                    rollback(record)
                raise
            return record

    def update(self, record: AssetRecord) -> AssetRecord:
        with _AssetIndexLock(self.run_paths.assets_index_lock_path):
            records = self.records(include_deleted=True)
            updated: list[AssetRecord] = []
            found = False
            for current in records:
                if current.id == record.id:
                    updated.append(record)
                    found = True
                else:
                    updated.append(current)
            if not found:
                raise AssetNotFoundError(f"Asset not found: {record.id}")
            self._write_records(updated)
            return record

    def delete(self, asset_id: str) -> AssetRecord:
        with _AssetIndexLock(self.run_paths.assets_index_lock_path):
            records = self.records(include_deleted=True)
            updated: list[AssetRecord] = []
            deleted: AssetRecord | None = None
            for current in records:
                if current.id == asset_id:
                    deleted = (
                        current if current.deleted_at else replace(current, deleted_at=now_iso())
                    )
                    updated.append(deleted)
                else:
                    updated.append(current)
            if deleted is None:
                raise AssetNotFoundError(f"Asset not found: {asset_id}")
            self._write_records(updated)
            return deleted

    def update_comprehension_version(
        self,
        asset_id: str,
        *,
        status: Literal["ready", "failed", "minimal"],
        version: int,
    ) -> AssetRecord:
        current = self.get(asset_id, include_deleted=True)
        return self.update(
            replace(
                current,
                comprehension_status=status,
                comprehension_current_version=version,
            )
        )

    def write_comprehension(self, record: ComprehensionRecord) -> ComprehensionRecord:
        with _AssetIndexLock(self.run_paths.assets_index_lock_path):
            asset = self.get(record.asset_id, include_deleted=True)
            current_version = max(
                int(asset.comprehension_current_version or 0),
                _latest_comprehension_version(self.run_paths, record.asset_id) or 0,
            )
            next_version = current_version + 1
            versioned = replace(record, version=next_version)
            version_path = asset_comprehension_version_path(
                self.run_paths,
                record.asset_id,
                next_version,
            )
            current_path = asset_comprehension_current_path(self.run_paths, record.asset_id)
            atomic_write_json(version_path, versioned.to_dict())
            records = self.records(include_deleted=True)
            updated = [
                replace(
                    item,
                    comprehension_status=versioned.status,
                    comprehension_current_version=next_version,
                )
                if item.id == record.asset_id
                else item
                for item in records
            ]
            self._write_records(updated)
            atomic_write_json(
                current_path,
                {
                    "asset_id": record.asset_id,
                    "updated_at": now_iso(),
                    "version": next_version,
                },
            )
            return versioned

    def read_comprehension(self, asset_id: str, version: int | None = None) -> ComprehensionRecord:
        if version is None:
            candidate_versions: list[int] = []
            pointer_path = asset_comprehension_current_path(self.run_paths, asset_id)
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pointer = None
            if pointer is not None:
                if not isinstance(pointer, dict):
                    raise AssetError(f"Invalid comprehension pointer: {pointer_path}")
                pointer_version = int(pointer.get("version") or 0)
                if (
                    pointer_version > 0
                    and asset_comprehension_version_path(
                        self.run_paths, asset_id, pointer_version
                    ).exists()
                ):
                    candidate_versions.append(pointer_version)
            try:
                asset_version = int(
                    self.get(asset_id, include_deleted=True).comprehension_current_version or 0
                )
            except AssetNotFoundError:
                asset_version = 0
            if (
                asset_version > 0
                and asset_comprehension_version_path(
                    self.run_paths, asset_id, asset_version
                ).exists()
            ):
                candidate_versions.append(asset_version)
            latest_version = _latest_comprehension_version(self.run_paths, asset_id)
            if latest_version is not None:
                candidate_versions.append(latest_version)
            if not candidate_versions:
                raise AssetNotFoundError(f"No comprehension exists for asset: {asset_id}")
            version = max(candidate_versions)
        if version <= 0:
            raise AssetNotFoundError(f"No comprehension version exists for asset: {asset_id}")
        path = asset_comprehension_version_path(self.run_paths, asset_id, version)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise AssetNotFoundError(
                f"Comprehension version not found: {asset_id} v{version}"
            ) from e
        if not isinstance(payload, dict):
            raise AssetError(f"Invalid comprehension record: {path}")
        return ComprehensionRecord.from_dict(payload)

    def find_existing_comprehension_by_sha256(
        self,
        sha256: str,
        *,
        model: str | None,
        role: str | None,
    ) -> ComprehensionRecord | None:
        digest = sha256.strip().lower()
        for asset in self.records(include_deleted=False):
            if asset.sha256 != digest:
                continue
            try:
                comprehension = self.read_comprehension(asset.id)
            except AssetError:
                continue
            if (
                comprehension.status == "ready"
                and comprehension.model == model
                and comprehension.role == role
            ):
                return comprehension
        return None

    def diff_comprehension(
        self,
        asset_id: str,
        version_a: int,
        version_b: int,
    ) -> dict[str, Any]:
        left = self.read_comprehension(asset_id, version_a)
        right = self.read_comprehension(asset_id, version_b)
        left_data = left.data
        right_data = right.data
        changed_fields: dict[str, dict[str, Any]] = {}
        for field_name in ("semantic_summary", "relations_hint", "classification", "confidence"):
            left_value = getattr(left_data, field_name)
            right_value = getattr(right_data, field_name)
            if left_value != right_value:
                changed_fields[field_name] = {"from": left_value, "to": right_value}
        return {
            "asset_id": asset_id,
            "version_a": version_a,
            "version_b": version_b,
            "changed_fields": changed_fields,
            "key_entities": _list_diff(left_data.key_entities, right_data.key_entities),
            "stated_facts": _list_diff(left_data.stated_facts, right_data.stated_facts),
            "stated_decisions": _list_diff(left_data.stated_decisions, right_data.stated_decisions),
            "stated_constraints": _list_diff(
                left_data.stated_constraints, right_data.stated_constraints
            ),
            "actionable_signals": _list_diff(
                left_data.actionable_signals,
                right_data.actionable_signals,
            ),
            "open_questions": _list_diff(left_data.open_questions, right_data.open_questions),
        }

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ASSET_INDEX_SCHEMA_VERSION,
            "run_id": self.run_paths.run_id,
            "updated_at": now_iso(),
            "assets": [],
            "by_sha256": {},
        }

    def _write_records(self, records: list[AssetRecord]) -> None:
        sorted_records = sorted(records, key=lambda item: (item.added_at, item.id))
        by_sha256 = {
            record.sha256: record.id
            for record in sorted_records
            if record.deleted_at is None and record.sha256
        }
        atomic_write_json(
            self.run_paths.assets_index_path,
            {
                "schema_version": ASSET_INDEX_SCHEMA_VERSION,
                "run_id": self.run_paths.run_id,
                "updated_at": now_iso(),
                "assets": [record.to_dict() for record in sorted_records],
                "by_sha256": by_sha256,
            },
        )

    @staticmethod
    def _find_active_by_sha(payload: dict[str, Any], sha256: str) -> AssetRecord | None:
        digest = sha256.strip().lower()
        for item in payload.get("assets", []):
            if not isinstance(item, dict):
                continue
            record = AssetRecord.from_dict(item)
            if record.deleted_at is None and record.sha256 == digest:
                return record
        return None


class _AssetIndexLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.recovery_path = path.with_name(f"{path.name}.recovering")
        self.owner_token = uuid.uuid4().hex
        self._process_lock = _process_lock_for_path(path) if os.name == "nt" else None
        self._process_lock_acquired = False

    def __enter__(self) -> _AssetIndexLock:
        if self._process_lock is not None:
            self._process_lock.acquire()
            self._process_lock_acquired = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = _build_lock_metadata(owner_token=self.owner_token, kind="lock")
            payload_text = _metadata_text(payload)
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                _clear_stale_recovery_claim(self.recovery_path)
                try:
                    _write_exclusive(self.path, payload_text)
                    return self
                except OSError as exc:
                    if not _is_existing_lock_contention(exc, self.path):
                        raise
                    existing_metadata, existing_text = _read_lock_metadata_with_text(self.path)
                    if (
                        existing_metadata is not None
                        and existing_text is not None
                        and _lock_metadata_is_stale(existing_metadata)
                        and self._recover_stale_lock(
                            existing_metadata=existing_metadata,
                            existing_text=existing_text,
                            replacement_text=payload_text,
                        )
                    ):
                        return self
                    if time.monotonic() >= deadline:
                        raise AssetError(
                            f"Timed out waiting for asset index lock: {self.path}"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
        except BaseException:
            self._release_process_lock()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type
        _ = exc
        _ = tb
        self.release()

    def release(self) -> None:
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    text = self.path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    return
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise AssetError(
                            f"Timed out releasing asset index lock: {self.path}"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS / 4)
                    continue
                try:
                    metadata = json.loads(text)
                except json.JSONDecodeError:
                    return
                if not isinstance(metadata, dict):
                    return
                if str(metadata.get("owner_token") or "") != self.owner_token:
                    return
                try:
                    self.path.unlink()
                    return
                except FileNotFoundError:
                    return
                except PermissionError:
                    # Windows readers do not share delete access by default. A
                    # waiter inspecting this lock can therefore make unlink
                    # fail briefly even after the creating file descriptor is
                    # closed.
                    if time.monotonic() >= deadline:
                        raise AssetError(
                            f"Timed out releasing asset index lock: {self.path}"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS / 4)
        finally:
            self._release_process_lock()

    def _release_process_lock(self) -> None:
        if not self._process_lock_acquired:
            return
        self._process_lock_acquired = False
        if self._process_lock is not None:
            self._process_lock.release()

    def _recover_stale_lock(
        self,
        *,
        existing_metadata: dict[str, Any],
        existing_text: str,
        replacement_text: str,
    ) -> bool:
        recovery_payload = _build_lock_metadata(
            owner_token=self.owner_token,
            kind="recovery",
            recovery_reason="stale asset index lock",
            observed_owner_token=str(existing_metadata.get("owner_token") or ""),
        )
        try:
            _write_exclusive(self.recovery_path, _metadata_text(recovery_payload))
        except OSError as exc:
            if not _is_existing_lock_contention(exc, self.recovery_path):
                raise
            _clear_stale_recovery_claim(self.recovery_path)
            return False
        try:
            current_metadata, current_text = _read_lock_metadata_with_text(self.path)
            if current_metadata is None or current_text != existing_text:
                return False
            if not _lock_metadata_is_stale(current_metadata):
                return False
            try:
                self.path.unlink()
            except FileNotFoundError:
                return False
            try:
                _write_exclusive(self.path, replacement_text)
            except OSError as exc:
                if not _is_existing_lock_contention(exc, self.path):
                    raise
                return False
            return True
        finally:
            recovery_metadata = _read_lock_metadata(self.recovery_path)
            if str((recovery_metadata or {}).get("owner_token") or "") == self.owner_token:
                with suppress(FileNotFoundError):
                    self.recovery_path.unlink()


def _write_exclusive(path: Path, text: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(path, flags, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with suppress(FileNotFoundError):
            path.unlink()
        raise


def _is_existing_lock_contention(error: OSError, path: Path) -> bool:
    """Return true when exclusive-create lost a race to an existing lock.

    POSIX reports ``EEXIST``/``FileExistsError`` for this case. Windows can
    instead report ``EACCES``/``PermissionError`` while the winning thread is
    still writing the newly created file. Treat that Windows-only shape as
    normal contention only when the target now exists; genuine permission
    failures on an absent target must continue to propagate.
    """

    return isinstance(error, FileExistsError) or (
        os.name == "nt" and isinstance(error, PermissionError) and path.exists()
    )


def _build_lock_metadata(
    *,
    owner_token: str,
    kind: str,
    recovery_reason: str | None = None,
    observed_owner_token: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "owner_token": owner_token,
        "acquired_at": now_iso(),
        "kind": kind,
    }
    if recovery_reason:
        payload["recovery_reason"] = recovery_reason
    if observed_owner_token:
        payload["observed_owner_token"] = observed_owner_token
    return payload


def _metadata_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _read_lock_metadata(path: Path) -> dict[str, Any] | None:
    metadata, _text = _read_lock_metadata_with_text(path)
    return metadata


def _read_lock_metadata_with_text(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None, None
    return (raw, text) if isinstance(raw, dict) else (None, text)


def _clear_stale_recovery_claim(path: Path) -> None:
    metadata = _read_lock_metadata(path)
    if metadata is not None and _lock_metadata_is_stale(metadata):
        with suppress(FileNotFoundError):
            path.unlink()


def _lock_is_stale(path: Path) -> bool:
    metadata = _read_lock_metadata(path)
    return metadata is not None and _lock_metadata_is_stale(metadata)


def _lock_metadata_is_stale(metadata: dict[str, Any]) -> bool:
    if not metadata or str(metadata.get("hostname") or "") != socket.gethostname():
        return False
    try:
        pid = int(metadata.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _latest_comprehension_version(run_paths: RunPaths, asset_id: str) -> int | None:
    directory = run_paths.assets_comprehensions_dir / asset_id
    try:
        candidates = list(directory.glob("v*.json"))
    except OSError:
        return None
    versions: list[int] = []
    for path in candidates:
        stem = path.stem
        if not stem.startswith("v"):
            continue
        try:
            version = int(stem[1:])
        except ValueError:
            continue
        if version > 0:
            versions.append(version)
    return max(versions) if versions else None


def _list_diff(left: list[Any], right: list[Any]) -> dict[str, list[Any]]:
    left_map = {_stable_item_key(item): item for item in left}
    right_map = {_stable_item_key(item): item for item in right}
    return {
        "added": [right_map[key] for key in sorted(set(right_map) - set(left_map))],
        "removed": [left_map[key] for key in sorted(set(left_map) - set(right_map))],
    }


def _stable_item_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
