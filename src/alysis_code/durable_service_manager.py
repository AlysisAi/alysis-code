from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .background_runner import _apply_env_overrides
from .branding import default_sandbox_docker_image
from .error_text import sanitize_optional_error_summary
from .sandbox_runner import (
    _SENSITIVE_ENV_KEYS,
    _build_bwrap_argv,
    _build_docker_argv,
    _docker_cleanup_container,
    _supports_bwrap_unshare_cgroup,
)
from .sandbox_settings import ShellSandboxSettings

SERVICE_SCHEMA_VERSION = 1
SERVICE_STDOUT_LOG = "stdout.log"
SERVICE_STDERR_LOG = "stderr.log"
SERVICE_METADATA = "service.json"
SERVICE_PREVIEW_READY = "preview-ready.json"
SERVICE_PREVIEW_TOKEN = "preview-token"
_STOP_TIMEOUT_S = 3.0
_KILL_TIMEOUT_S = 1.0


class ProcessOwnership(StrEnum):
    SESSION = "SESSION"
    DURABLE_SERVICE = "DURABLE_SERVICE"


class DurableServiceStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    STOPPED = "stopped"
    STALE = "stale"
    UNKNOWN = "unknown"


class ReadinessStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DurableServiceStart:
    service_id: str
    payload: dict[str, Any]


class DurableServiceManager:
    def __init__(
        self,
        *,
        root: Path,
        state_dir: Path,
        settings: ShellSandboxSettings,
    ) -> None:
        self.root = root.resolve()
        self.state_dir = state_dir.resolve()
        self.settings = settings
        self._popens: dict[str, subprocess.Popen[bytes]] = {}

    def start(
        self,
        *,
        cmd: str,
        cwd: Path,
        readiness: dict[str, Any] | None = None,
    ) -> DurableServiceStart:
        cleaned_cmd = str(cmd or "").strip()
        if not cleaned_cmd:
            raise ValueError("cmd cannot be empty")
        cwd_abs = cwd.resolve()
        try:
            cwd_abs.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"cwd escapes root: {cwd}") from exc

        readiness_spec = normalize_readiness_spec(readiness)
        return self._start_prepared(
            cleaned_cmd=cleaned_cmd,
            cwd_abs=cwd_abs,
            readiness_spec=readiness_spec,
            launch_builder=lambda service_id: self._build_launch(
                cmd=cleaned_cmd,
                cwd=cwd_abs,
                service_id=service_id,
                readiness=readiness_spec,
            ),
        )

    def resolve_preview_access(self, requested: object = "auto") -> str:
        return _resolve_preview_access(
            requested,
            configured=getattr(self.settings, "preview_access", "auto"),
        )

    def start_preview(
        self,
        *,
        cwd: Path,
        access: str = "auto",
        port: int | None = None,
    ) -> DurableServiceStart:
        """Start a constrained static preview using semantic network access."""

        cwd_abs = cwd.resolve()
        try:
            cwd_abs.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"cwd escapes root: {cwd}") from exc
        if not cwd_abs.is_dir():
            raise ValueError(f"preview cwd is not a directory: {cwd}")
        if port is not None and (
            isinstance(port, bool) or not isinstance(port, int) or port <= 0 or port > 65535
        ):
            raise ValueError("preview port must be between 1 and 65535")
        effective_access = self.resolve_preview_access(access)
        token = secrets.token_urlsafe(32) if effective_access == "lan" else None
        readiness_spec = normalize_readiness_spec(
            {
                "type": "preview_ready",
                "timeout_s": 10.0,
                "interval_s": 0.1,
            }
        )

        def _preview_launch(service_id: str) -> dict[str, Any]:
            service_dir = self._service_dir(service_id)
            ready_path = service_dir / SERVICE_PREVIEW_READY
            token_path = service_dir / SERVICE_PREVIEW_TOKEN
            argv = [
                sys.executable,
                "-m",
                "alysis_code.preview_server",
                "--root",
                os.fspath(cwd_abs),
                "--access",
                effective_access,
                "--ready-file",
                os.fspath(ready_path),
            ]
            if port is not None:
                argv.extend(["--port", str(port)])
            if token is not None:
                _write_private_text(token_path, token)
                argv.extend(["--token-file", os.fspath(token_path)])
            return {
                "backend": "host-preview",
                "popen_args": argv,
                "popen_cwd": self.root,
                "shell": False,
                "env": _safe_parent_env(),
            }

        return self._start_prepared(
            cleaned_cmd=f"alysis-workspace-preview:{effective_access}",
            cwd_abs=cwd_abs,
            readiness_spec=readiness_spec,
            launch_builder=_preview_launch,
            metadata_extra={
                "preview_access": effective_access,
                "preview_authentication_required": token is not None,
            },
            metadata_from_readiness=_preview_metadata_from_readiness,
        )

    def _start_prepared(
        self,
        *,
        cleaned_cmd: str,
        cwd_abs: Path,
        readiness_spec: dict[str, Any],
        launch_builder: Callable[[str], dict[str, Any]],
        metadata_extra: dict[str, Any] | None = None,
        metadata_from_readiness: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> DurableServiceStart:
        service_id = f"svc_{uuid.uuid4().hex[:16]}"
        service_dir = self._service_dir(service_id)
        service_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = service_dir / SERVICE_STDOUT_LOG
        stderr_path = service_dir / SERVICE_STDERR_LOG
        metadata_path = service_dir / SERVICE_METADATA
        launch = launch_builder(service_id)

        started_at = time.time()
        try:
            with (
                stdout_path.open("ab", buffering=0) as stdout_fh,
                stderr_path.open("ab", buffering=0) as stderr_fh,
            ):
                popen = subprocess.Popen(
                    launch["popen_args"],
                    shell=bool(launch["shell"]),
                    cwd=str(launch["popen_cwd"]),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    env=launch["env"],
                    start_new_session=(os.name != "nt"),
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                    close_fds=True,
                )
        except BaseException:
            self._safe_unlink(metadata_path)
            self._safe_unlink(service_dir / SERVICE_PREVIEW_READY)
            self._safe_unlink(service_dir / SERVICE_PREVIEW_TOKEN)
            raise

        self._popens[service_id] = popen
        metadata = {
            "schema_version": SERVICE_SCHEMA_VERSION,
            "service_id": service_id,
            "ownership": ProcessOwnership.DURABLE_SERVICE.value,
            "status": DurableServiceStatus.RUNNING.value,
            "pid": popen.pid,
            "pgid": _process_group_id(popen.pid),
            "pid_start_token": _pid_start_token(popen.pid),
            "started_at_wall": started_at,
            "root": os.fspath(self.root),
            "cwd": os.fspath(cwd_abs),
            "backend": str(launch["backend"]),
            "container_name": launch.get("container_name"),
            "cmd_sha256": hashlib.sha256(cleaned_cmd.encode("utf-8")).hexdigest(),
            "readiness": readiness_spec,
            "stdout_log_path": os.fspath(stdout_path),
            "stderr_log_path": os.fspath(stderr_path),
        }
        metadata.update(metadata_extra or {})
        self._write_metadata(metadata)
        readiness_payload = self._check_readiness(
            metadata=metadata,
            timeout_s=_readiness_timeout(readiness_spec),
        )
        if readiness_payload["status"] != ReadinessStatus.READY.value:
            self._terminate_loaded_metadata(metadata, remove_metadata=False)
            status_payload = self.status(service_id)
            self._safe_unlink(service_dir / SERVICE_PREVIEW_TOKEN)
            status_payload.pop("access_url", None)
            status_payload["failure_category"] = "readiness_failed"
            status_payload["readiness"] = readiness_payload
            startup_error = _service_startup_error(stderr_path=stderr_path, stdout_path=stdout_path)
            if startup_error:
                status_payload["startup_error"] = startup_error
            return DurableServiceStart(service_id=service_id, payload=status_payload)
        if metadata_from_readiness is not None:
            try:
                metadata.update(metadata_from_readiness(readiness_payload))
                self._write_metadata(metadata)
            except BaseException:
                self._terminate_loaded_metadata(metadata, remove_metadata=False)
                self._safe_unlink(service_dir / SERVICE_PREVIEW_TOKEN)
                raise
        return DurableServiceStart(service_id=service_id, payload=self.status(service_id))

    def status(self, service_id: str) -> dict[str, Any]:
        metadata = self._read_metadata(service_id)
        if metadata is None:
            return {
                "service_id": service_id,
                "ownership": ProcessOwnership.DURABLE_SERVICE.value,
                "status": DurableServiceStatus.UNKNOWN.value,
                "alive": False,
                "readiness": {
                    "type": "process_alive",
                    "status": ReadinessStatus.FAILED.value,
                    "detail": "metadata not found",
                },
                "failure_category": "unknown_service",
            }
        alive, identity_valid = self._metadata_process_alive(metadata)
        status = (
            DurableServiceStatus.RUNNING.value
            if alive
            else DurableServiceStatus.STALE.value
            if not identity_valid
            else DurableServiceStatus.EXITED.value
        )
        metadata["status"] = status
        self._write_metadata(metadata)
        readiness = self._check_readiness(
            metadata=metadata,
            timeout_s=_readiness_timeout(dict(metadata.get("readiness") or {})),
        )
        payload = _metadata_public_payload(
            metadata,
            status=status,
            alive=alive,
            identity_valid=identity_valid,
            readiness=readiness,
        )
        return self._attach_preview_access_url(payload, metadata)

    def stop(self, service_id: str) -> dict[str, Any]:
        metadata = self._read_metadata(service_id)
        if metadata is None:
            return {
                "service_id": service_id,
                "ownership": ProcessOwnership.DURABLE_SERVICE.value,
                "status": DurableServiceStatus.UNKNOWN.value,
                "stopped": False,
                "failure_category": "unknown_service",
            }
        alive, identity_valid = self._metadata_process_alive(metadata)
        if alive and not identity_valid:
            payload = _metadata_public_payload(
                metadata,
                status=DurableServiceStatus.STALE.value,
                alive=False,
                identity_valid=False,
                readiness={
                    "type": "process_alive",
                    "status": ReadinessStatus.FAILED.value,
                    "detail": "pid identity does not match durable service metadata",
                },
            )
            payload["stopped"] = False
            payload["failure_category"] = "stale_metadata_identity_mismatch"
            metadata["status"] = DurableServiceStatus.STALE.value
            self._write_metadata(metadata)
            return payload
        if alive:
            self._terminate_loaded_metadata(metadata, remove_metadata=True)
        else:
            self._remove_metadata(metadata)
        payload = _metadata_public_payload(
            metadata,
            status=DurableServiceStatus.STOPPED.value,
            alive=False,
            identity_valid=identity_valid,
            readiness={
                "type": str(dict(metadata.get("readiness") or {}).get("type") or "process_alive"),
                "status": ReadinessStatus.FAILED.value,
                "detail": "service stopped",
            },
        )
        payload["stopped"] = True
        return payload

    def list_active(self) -> list[dict[str, Any]]:
        if not self.state_dir.exists():
            return []
        services: list[dict[str, Any]] = []
        for metadata_path in sorted(self.state_dir.glob(f"*/{SERVICE_METADATA}")):
            service_id = metadata_path.parent.name
            payload = self.status(service_id)
            if payload.get("status") == DurableServiceStatus.RUNNING.value:
                services.append(payload)
        return services

    def _build_launch(
        self,
        *,
        cmd: str,
        cwd: Path,
        service_id: str,
        readiness: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.settings.mode == "off":
            return {
                "backend": "host",
                "popen_args": cmd,
                "popen_cwd": cwd,
                "shell": True,
                "env": _safe_parent_env(),
            }

        backend = self.settings.backend
        has_bwrap = platform.system().lower() == "linux" and shutil.which("bwrap") is not None
        has_docker = shutil.which("docker") is not None
        if backend in {"auto", "bwrap"} and has_bwrap:
            argv, env = _build_bwrap_argv(
                root=self.root,
                cwd=cwd,
                cmd=cmd,
                network=self.settings.network,
                clear_env=self.settings.clear_env,
                profile=self.settings.bwrap_profile,
                unshare_cgroup=_supports_bwrap_unshare_cgroup(),
            )
            argv = [item for item in argv if item != "--die-with-parent"]
            return {
                "backend": "bwrap",
                "popen_args": argv,
                "popen_cwd": self.root,
                "shell": False,
                "env": env,
            }
        if backend in {"auto", "docker"} and has_docker:
            published_ports: tuple[tuple[str, int, int], ...] = ()
            readiness_spec = readiness or {}
            if str(readiness_spec.get("type") or "") == "tcp":
                host = str(readiness_spec.get("host") or "localhost").strip()
                port = int(readiness_spec.get("port") or 0)
                if not _host_resolves_only_to_loopback(host):
                    raise RuntimeError(
                        "Docker durable-service TCP readiness must resolve to a local loopback "
                        "interface. Use the workspace preview access policy for broader exposure."
                    )
                if self.settings.network == "off":
                    raise RuntimeError(
                        "Docker networking is disabled. Use workspace_preview_start for static "
                        "localhost previews, or explicitly enable shell sandbox networking for "
                        "a containerized development server."
                    )
                published_ports = ((_system_loopback_address(), port, port),)
            container_name = f"alysis-svc-{service_id[-12:]}"
            argv, env = _build_docker_argv(
                root=self.root,
                cwd=cwd,
                cmd=cmd,
                container_name=container_name,
                network=self.settings.network,
                docker_image=self.settings.docker_image or default_sandbox_docker_image("dev"),
                clear_env=self.settings.clear_env,
                pids_limit=self.settings.docker_pids_limit,
                memory_limit=self.settings.docker_memory,
                cpus=self.settings.docker_cpus,
                read_only_rootfs=self.settings.docker_read_only,
                protect_repo_meta=self.settings.protect_repo_meta,
                env_allowlist=self.settings.docker_env_allowlist,
                published_ports=published_ports,
            )
            return {
                "backend": "docker",
                "container_name": container_name,
                "popen_args": argv,
                "popen_cwd": self.root,
                "shell": False,
                "env": env,
            }
        if self.settings.mode == "warn":
            raise RuntimeError(
                "Durable service sandbox unavailable; host fallback is disabled for durable services."
            )
        raise RuntimeError(
            "Shell sandbox strict mode is enabled, but no usable durable service backend is available."
        )

    def _check_readiness(
        self,
        *,
        metadata: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        readiness = dict(metadata.get("readiness") or {})
        readiness_type = str(readiness.get("type") or "process_alive")
        deadline = time.monotonic() + max(0.0, timeout_s)
        interval_s = max(0.02, min(float(readiness.get("interval_s") or 0.1), 1.0))
        last_detail = ""
        while True:
            alive, identity_valid = self._metadata_process_alive(metadata)
            if readiness_type == "process_alive":
                status = (
                    ReadinessStatus.READY if alive and identity_valid else ReadinessStatus.FAILED
                )
                return {
                    "type": readiness_type,
                    "status": status.value,
                    "detail": "process is alive"
                    if status == ReadinessStatus.READY
                    else "process is not alive",
                }
            if not alive or not identity_valid:
                return {
                    "type": readiness_type,
                    "status": ReadinessStatus.FAILED.value,
                    "detail": "process is not alive",
                }
            if readiness_type == "tcp":
                host = str(readiness.get("host") or "localhost")
                port = int(readiness.get("port") or 0)
                try:
                    with socket.create_connection((host, port), timeout=min(interval_s, 0.5)):
                        return {
                            "type": readiness_type,
                            "status": ReadinessStatus.READY.value,
                            "host": host,
                            "port": port,
                            "detail": "tcp connection succeeded",
                        }
                except OSError as exc:
                    last_detail = str(exc)
            elif readiness_type == "preview_ready":
                service_id = str(metadata.get("service_id") or "")
                ready_path = self._service_dir(service_id) / SERVICE_PREVIEW_READY
                try:
                    ready = _read_preview_ready(
                        ready_path,
                        expected_access=str(metadata.get("preview_access") or ""),
                    )
                except (OSError, ValueError) as exc:
                    last_detail = str(exc)
                else:
                    host = str(ready["probe_host"])
                    port = int(ready["port"])
                    try:
                        with socket.create_connection((host, port), timeout=min(interval_s, 0.5)):
                            return {
                                "type": readiness_type,
                                "status": ReadinessStatus.READY.value,
                                "host": host,
                                "port": port,
                                "preview_urls": list(ready["preview_urls"]),
                                "access": str(ready["access"]),
                                "runtime": str(ready["runtime"]),
                                "authentication_required": bool(ready["authentication_required"]),
                                "detail": "preview server reported ready and accepted TCP",
                            }
                    except OSError as exc:
                        last_detail = str(exc)
            elif readiness_type == "unix_socket":
                path = Path(str(readiness.get("path") or ""))
                if path.exists():
                    return {
                        "type": readiness_type,
                        "status": ReadinessStatus.READY.value,
                        "path": os.fspath(path),
                        "detail": "unix socket exists",
                    }
                last_detail = "unix socket does not exist"
            elif readiness_type == "command":
                command = str(readiness.get("command") or "").strip()
                if not command:
                    return {
                        "type": readiness_type,
                        "status": ReadinessStatus.FAILED.value,
                        "detail": "readiness command is empty",
                    }
                try:
                    result = subprocess.run(
                        command,
                        shell=True,
                        cwd=str(metadata.get("cwd") or self.root),
                        env=_safe_parent_env(),
                        capture_output=True,
                        text=True,
                        timeout=min(max(0.1, timeout_s), 5.0),
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    last_detail = "readiness command timed out"
                else:
                    if result.returncode == 0:
                        return {
                            "type": readiness_type,
                            "status": ReadinessStatus.READY.value,
                            "exit_code": result.returncode,
                            "detail": "readiness command passed",
                        }
                    last_detail = f"readiness command exited {result.returncode}"
            else:
                return {
                    "type": readiness_type,
                    "status": ReadinessStatus.UNSUPPORTED.value,
                    "detail": f"unsupported readiness type: {readiness_type}",
                }

            if time.monotonic() >= deadline:
                return {
                    "type": readiness_type,
                    "status": ReadinessStatus.FAILED.value,
                    "detail": last_detail or "readiness probe timed out",
                }
            time.sleep(interval_s)

    def _metadata_process_alive(self, metadata: dict[str, Any]) -> tuple[bool, bool]:
        pid = int(metadata.get("pid") or 0)
        if pid <= 0:
            return False, False
        expected_token = str(metadata.get("pid_start_token") or "")
        if expected_token:
            current_token = _pid_start_token(pid)
            if current_token and current_token != expected_token:
                return True, False
        popen = self._popens.get(str(metadata.get("service_id") or ""))
        if popen is not None and popen.poll() is not None:
            return False, True
        return _pid_exists(pid), True

    def _terminate_loaded_metadata(
        self,
        metadata: dict[str, Any],
        *,
        remove_metadata: bool,
    ) -> None:
        service_id = str(metadata.get("service_id") or "")
        popen = self._popens.get(service_id)
        if str(metadata.get("backend") or "") == "docker" and metadata.get("container_name"):
            _docker_cleanup_container(
                str(metadata["container_name"]),
                cwd=os.fspath(self.root),
                env=_safe_parent_env(),
                warning_callback=None,
                reason="durable service stop",
                quiet=True,
            )
        if popen is not None:
            if popen.poll() is None:
                _terminate_popen_tree(popen, timeout_s=_STOP_TIMEOUT_S)
        else:
            pid = int(metadata.get("pid") or 0)
            pgid = metadata.get("pgid")
            _terminate_pid_or_group(pid=pid, pgid=pgid, timeout_s=_STOP_TIMEOUT_S)
        metadata["status"] = DurableServiceStatus.STOPPED.value
        if remove_metadata:
            self._remove_metadata(metadata)
        else:
            self._write_metadata(metadata)

    def _read_metadata(self, service_id: str) -> dict[str, Any] | None:
        normalized = _normalize_service_id(service_id)
        if not normalized:
            return None
        path = self._service_dir(normalized) / SERVICE_METADATA
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("service_id") != normalized:
            return None
        return raw

    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        service_id = str(metadata.get("service_id") or "")
        service_dir = self._service_dir(service_id)
        service_dir.mkdir(parents=True, exist_ok=True)
        path = service_dir / SERVICE_METADATA
        path.write_text(
            json.dumps(_sanitize_metadata(metadata), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _remove_metadata(self, metadata: dict[str, Any]) -> None:
        service_id = str(metadata.get("service_id") or "")
        service_dir = self._service_dir(service_id)
        self._safe_unlink(service_dir / SERVICE_METADATA)
        self._safe_unlink(service_dir / SERVICE_PREVIEW_READY)
        self._safe_unlink(service_dir / SERVICE_PREVIEW_TOKEN)

    def _attach_preview_access_url(
        self,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        preview_url = str(payload.get("preview_url") or "").strip()
        if not preview_url:
            return payload
        if not bool(metadata.get("preview_authentication_required")):
            payload["access_url"] = preview_url
            return payload
        service_id = str(metadata.get("service_id") or "")
        try:
            token = (
                (self._service_dir(service_id) / SERVICE_PREVIEW_TOKEN)
                .read_text(encoding="utf-8")
                .strip()
            )
        except OSError:
            return payload
        if token:
            payload["access_url"] = _url_with_preview_token(preview_url, token)
        return payload

    def _service_dir(self, service_id: str) -> Path:
        normalized = _normalize_service_id(service_id)
        if not normalized:
            raise ValueError("invalid service_id")
        return self.state_dir / normalized

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return


def normalize_readiness_spec(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {"type": "process_alive", "timeout_s": 1.0, "interval_s": 0.1}
    if not isinstance(raw, dict):
        return {"type": "process_alive", "timeout_s": 1.0, "interval_s": 0.1}
    readiness_type = str(raw.get("type") or "process_alive").strip().lower()
    out: dict[str, Any] = {
        "type": readiness_type,
        "timeout_s": _bounded_float(raw.get("timeout_s"), default=5.0, low=0.0, high=30.0),
        "interval_s": _bounded_float(raw.get("interval_s"), default=0.1, low=0.02, high=2.0),
    }
    if readiness_type == "tcp":
        out["host"] = str(raw.get("host") or "localhost")
        out["port"] = int(raw.get("port") or 0)
    elif readiness_type == "unix_socket":
        out["path"] = str(raw.get("path") or "")
    elif readiness_type == "command":
        out["command"] = str(raw.get("command") or "")
    return out


def _resolve_preview_access(requested: object, *, configured: object) -> str:
    requested_value = str(requested or "auto").strip().lower()
    configured_value = str(configured or "auto").strip().lower()
    allowed = {"auto", "local", "lan"}
    if requested_value not in allowed:
        raise ValueError(f"preview access must be one of: {', '.join(sorted(allowed))}")
    if configured_value not in allowed:
        configured_value = "auto"
    effective = configured_value if requested_value == "auto" else requested_value
    # Auto is deliberately semantic. The current runtime policy selects a local
    # preview, while keeping the tool contract stable for future remote runtimes.
    return "local" if effective == "auto" else effective


def _host_resolves_only_to_loopback(host: str) -> bool:
    cleaned = str(host or "").strip()
    if not cleaned:
        return False
    try:
        addresses = socket.getaddrinfo(
            cleaned,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for candidate in addresses:
        raw_address = str(candidate[4][0]).split("%", 1)[0]
        try:
            resolved.append(ipaddress.ip_address(raw_address))
        except ValueError:
            return False
    return bool(resolved) and all(address.is_loopback for address in resolved)


def _system_loopback_address() -> str:
    candidates = socket.getaddrinfo(
        "localhost",
        None,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    # Docker Desktop and WSL consistently support IPv4 host publication even
    # when their IPv6 bridge is disabled. We still use the address returned by
    # the operating system rather than embedding one in the service policy.
    candidates.sort(key=lambda candidate: candidate[0] != socket.AF_INET)
    for candidate in candidates:
        raw_address = str(candidate[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            continue
        if address.is_loopback:
            return address.compressed
    raise RuntimeError("The operating system did not provide a local loopback address")


def _preview_metadata_from_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    urls = [str(item) for item in readiness.get("preview_urls") or [] if str(item).strip()]
    if not urls:
        raise ValueError("preview readiness did not provide a reachable URL")
    return {
        "preview_url": urls[0],
        "preview_urls": urls,
        "preview_port": int(readiness.get("port") or 0),
        "preview_runtime": str(readiness.get("runtime") or "unknown"),
    }


def _read_preview_ready(path: Path, *, expected_access: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OSError("preview readiness file has not been created") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("preview readiness file is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("preview readiness payload must be an object")
    access = str(raw.get("access") or "").strip().lower()
    if access not in {"local", "lan"} or access != expected_access:
        raise ValueError("preview readiness access does not match the requested policy")
    try:
        port = int(raw.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("preview readiness port is invalid") from exc
    if port <= 0 or port > 65535:
        raise ValueError("preview readiness port is out of range")
    probe_host = str(raw.get("probe_host") or "").strip()
    if not probe_host or not _host_resolves_only_to_loopback(probe_host):
        raise ValueError("preview readiness probe host must resolve only to loopback")
    authentication_required = bool(raw.get("authentication_required"))
    if authentication_required != (access == "lan"):
        raise ValueError("preview readiness authentication does not match the access policy")
    urls: list[str] = []
    for candidate in raw.get("preview_urls") or []:
        value = str(candidate or "").strip()
        parsed = urlsplit(value)
        try:
            parsed_port = parsed.port
        except ValueError:
            continue
        if parsed.scheme != "http" or not parsed.hostname or parsed_port != port:
            continue
        urls.append(value)
    if not urls:
        raise ValueError("preview readiness URLs are invalid")
    return {
        "access": access,
        "port": port,
        "probe_host": probe_host,
        "preview_urls": list(dict.fromkeys(urls)),
        "runtime": str(raw.get("runtime") or "unknown"),
        "authentication_required": authentication_required,
    }


def _write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _url_with_preview_token(url: str, token: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "alysis_token"]
    query.append(("alysis_token", token))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _readiness_timeout(readiness: dict[str, Any]) -> float:
    return _bounded_float(readiness.get("timeout_s"), default=5.0, low=0.0, high=30.0)


def _bounded_float(value: Any, *, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _safe_parent_env() -> dict[str, str]:
    blocked = {key.upper() for key in _SENSITIVE_ENV_KEYS}
    return _apply_env_overrides(
        {key: value for key, value in os.environ.items() if key.upper() not in blocked},
        None,
    )


def _service_startup_error(*, stderr_path: Path, stdout_path: Path) -> str | None:
    for path in (stderr_path, stdout_path):
        try:
            raw = path.read_bytes()[-16_384:]
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        summary = sanitize_optional_error_summary(text, max_chars=1000)
        if summary:
            return summary
    return None


def _normalize_service_id(service_id: str) -> str:
    cleaned = str(service_id or "").strip()
    if not cleaned or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in cleaned):
        return ""
    return cleaned


def _metadata_public_payload(
    metadata: dict[str, Any],
    *,
    status: str,
    alive: bool,
    identity_valid: bool,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "service_id": metadata.get("service_id"),
        "ownership": metadata.get("ownership") or ProcessOwnership.DURABLE_SERVICE.value,
        "status": status,
        "alive": bool(alive),
        "identity_valid": bool(identity_valid),
        "pid": metadata.get("pid"),
        "pgid": metadata.get("pgid"),
        "backend": metadata.get("backend"),
        "readiness": readiness,
        "start_timestamp": metadata.get("started_at_wall"),
        "stdout_log_path": metadata.get("stdout_log_path"),
        "stderr_log_path": metadata.get("stderr_log_path"),
        "log_paths": {
            "stdout": metadata.get("stdout_log_path"),
            "stderr": metadata.get("stderr_log_path"),
        },
        "failure_category": None
        if status == DurableServiceStatus.RUNNING.value
        and readiness.get("status") == ReadinessStatus.READY.value
        else "service_not_ready",
    }
    preview_url = str(metadata.get("preview_url") or "").strip()
    if preview_url:
        payload["preview_url"] = preview_url
    preview_urls = [str(item) for item in metadata.get("preview_urls") or [] if str(item).strip()]
    if preview_urls:
        payload["preview_urls"] = preview_urls
    preview_access = str(metadata.get("preview_access") or "").strip()
    if preview_access:
        payload["preview_access"] = preview_access
        payload["authentication_required"] = bool(metadata.get("preview_authentication_required"))
        payload["preview_runtime"] = str(metadata.get("preview_runtime") or "unknown")
        payload["preview_port"] = int(metadata.get("preview_port") or 0)
    return payload


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "schema_version",
        "service_id",
        "ownership",
        "status",
        "pid",
        "pgid",
        "pid_start_token",
        "started_at_wall",
        "root",
        "cwd",
        "backend",
        "container_name",
        "cmd_sha256",
        "readiness",
        "stdout_log_path",
        "stderr_log_path",
        "preview_url",
        "preview_urls",
        "preview_access",
        "preview_authentication_required",
        "preview_port",
        "preview_runtime",
    }
    return {key: metadata[key] for key in sorted(allowed_keys) if key in metadata}


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        # ``os.kill(pid, 0)`` is not a non-mutating existence probe on Windows:
        # CPython implements unsupported signals through TerminateProcess, so it
        # can kill the service while it is still starting. Query its exit status
        # through a read-only process handle instead.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code_process = kernel32.GetExitCodeProcess
        get_exit_code_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code_process.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code_process(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _pid_start_token(pid: int) -> str | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        after_comm = text.rsplit(") ", 1)[1]
        fields = after_comm.split()
        return f"linux:{fields[19]}"
    except (IndexError, ValueError):
        return None


def _process_group_id(pid: int) -> int | None:
    if os.name == "nt":
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _terminate_popen_tree(popen: subprocess.Popen[bytes], *, timeout_s: float) -> None:
    if popen.poll() is not None:
        return
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        _terminate_windows_process_tree(pid=popen.pid, timeout_s=timeout_s)
    else:
        pgid = _process_group_id(popen.pid)
        if pgid is not None:
            with _ignore_process_lookup():
                os.killpg(pgid, signal.SIGTERM)
        else:
            with _ignore_process_lookup():
                popen.terminate()
    try:
        popen.wait(timeout=timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        _terminate_windows_process_tree(pid=popen.pid, timeout_s=_KILL_TIMEOUT_S)
    else:
        pgid = _process_group_id(popen.pid)
        if pgid is not None:
            with _ignore_process_lookup():
                os.killpg(pgid, signal.SIGKILL)
        else:
            with _ignore_process_lookup():
                popen.kill()
    with _ignore_timeout():
        popen.wait(timeout=_KILL_TIMEOUT_S)


def _terminate_pid_or_group(*, pid: int, pgid: Any, timeout_s: float) -> None:
    if pid <= 0:
        return
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        _terminate_windows_process_tree(pid=pid, timeout_s=timeout_s)
        return
    _signal_pid_or_group(pid=pid, pgid=pgid, sig=signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        if _pid_is_zombie(pid):
            _signal_pid_or_group(pid=pid, pgid=pgid, sig=signal.SIGKILL)
            return
        time.sleep(0.05)
    _signal_pid_or_group(pid=pid, pgid=pgid, sig=signal.SIGKILL)


def _terminate_windows_process_tree(*, pid: int, timeout_s: float) -> None:
    if pid <= 0:
        return
    _ = timeout_s
    import ctypes
    from ctypes import wintypes

    th32cs_snapprocess = 0x00000002
    process_terminate = 0x0001
    invalid_handle_value = ctypes.c_void_p(-1).value

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_next.restype = wintypes.BOOL
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    children_by_parent: dict[int, list[int]] = {}
    snapshot = create_snapshot(th32cs_snapprocess, 0)
    if snapshot and snapshot != invalid_handle_value:
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            if process_first(snapshot, ctypes.byref(entry)):
                while True:
                    child_pid = int(entry.th32ProcessID)
                    parent_pid = int(entry.th32ParentProcessID)
                    children_by_parent.setdefault(parent_pid, []).append(child_pid)
                    if not process_next(snapshot, ctypes.byref(entry)):
                        break
        finally:
            close_handle(snapshot)

    ordered: list[int] = []
    pending = [pid]
    while pending:
        current = pending.pop()
        ordered.append(current)
        pending.extend(children_by_parent.get(current, ()))

    for target_pid in reversed(ordered):
        handle = open_process(process_terminate, False, target_pid)
        if not handle:
            continue
        try:
            terminate_process(handle, 1)
        finally:
            close_handle(handle)


def _signal_pid_or_group(*, pid: int, pgid: Any, sig: int) -> None:
    if os.name != "nt" and isinstance(pgid, int) and pgid > 0:
        try:
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            # On macOS, an exited-but-unreaped process may keep its PID while its
            # process group is no longer signalable. Fall back to the leader PID;
            # a genuine permission failure still propagates from this call.
            pass
    with _ignore_process_lookup():
        os.kill(pid, sig)


def _pid_is_zombie(pid: int) -> bool:
    if os.name == "nt" or pid <= 0:
        return False
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        try:
            state = text.rsplit(") ", 1)[1].split(maxsplit=1)[0]
        except (IndexError, ValueError):
            state = ""
        if state:
            return state == "Z"

    ps = shutil.which("ps")
    if not ps:
        return False
    try:
        completed = subprocess.run(
            [ps, "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    state = completed.stdout.strip().split(maxsplit=1)[0] if completed.stdout.strip() else ""
    return state.startswith("Z")


class _ignore_process_lookup:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, ProcessLookupError)


class _ignore_timeout:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, subprocess.TimeoutExpired)
