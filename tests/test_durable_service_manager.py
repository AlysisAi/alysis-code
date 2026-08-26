from __future__ import annotations

import http.cookiejar
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from alysis_code.durable_service_manager import (
    SERVICE_METADATA,
    SERVICE_PREVIEW_TOKEN,
    DurableServiceManager,
    _pid_exists,
    _terminate_windows_process_tree,
)
from alysis_code.sandbox_settings import ShellSandboxSettings


def _settings() -> ShellSandboxSettings:
    return ShellSandboxSettings(mode="off")


def _manager(root: Path, state_dir: Path) -> DurableServiceManager:
    return DurableServiceManager(root=root, state_dir=state_dir, settings=_settings())


def _shell_join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _python_cmd(code: str) -> str:
    return _shell_join([sys.executable, "-c", code])


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_http_ready(port: int) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
        assert response.status == 200


def _wait_loaded_popen(manager: DurableServiceManager, service_id: str) -> None:
    popen = manager._popens.get(service_id)
    if popen is not None:
        popen.wait(timeout=3)


def test_durable_http_service_survives_manager_recreation_and_fresh_stop(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    (root / "index.html").write_bytes(b"ok\n")
    port = _free_tcp_port()
    manager = _manager(root, state_dir)
    service_id = ""

    try:
        started = manager.start(
            cmd=_shell_join(
                [
                    sys.executable,
                    "-m",
                    "http.server",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                ]
            ),
            cwd=root,
            readiness={
                "type": "tcp",
                "host": "127.0.0.1",
                "port": port,
                "timeout_s": 5,
            },
        )
        service_id = started.service_id

        assert started.payload["ownership"] == "DURABLE_SERVICE"
        assert started.payload["status"] == "running"
        assert started.payload["readiness"]["status"] == "ready"
        _assert_http_ready(port)

        fresh_manager = _manager(root, state_dir)
        status = fresh_manager.status(service_id)
        assert status["status"] == "running"
        assert status["readiness"]["status"] == "ready"

        stopped = fresh_manager.stop(service_id)
        _wait_loaded_popen(manager, service_id)
        assert stopped["stopped"] is True
        assert not (state_dir / service_id / SERVICE_METADATA).exists()
    finally:
        if service_id:
            manager.stop(service_id)


def test_tcp_readiness_failure_stops_process_without_leak(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    manager = _manager(root, state_dir)
    port = _free_tcp_port()

    started = manager.start(
        cmd=_python_cmd("import time; time.sleep(30)"),
        cwd=root,
        readiness={
            "type": "tcp",
            "host": "127.0.0.1",
            "port": port,
            "timeout_s": 0.2,
            "interval_s": 0.05,
        },
    )

    try:
        assert started.payload["failure_category"] == "readiness_failed"
        assert started.payload["alive"] is False
        _wait_loaded_popen(manager, started.service_id)
    finally:
        manager.stop(started.service_id)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
def test_windows_status_is_non_mutating_and_stop_reaps_shell_child(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    child_pid_path = tmp_path / "child.pid"
    root.mkdir()
    manager = _manager(root, state_dir)
    service_id = ""
    child_pid = 0

    try:
        started = manager.start(
            cmd=_python_cmd(
                "from pathlib import Path; import os, time; "
                f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), "
                "encoding='ascii'); time.sleep(30)"
            ),
            cwd=root,
            readiness={"type": "process_alive", "timeout_s": 2},
        )
        service_id = started.service_id

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
            except (FileNotFoundError, ValueError):
                time.sleep(0.02)
                continue
            break
        assert child_pid > 0

        # Repeated status calls exercise the Windows liveness probe. They must
        # not signal either the shell launcher or its Python child.
        for _ in range(25):
            status = manager.status(service_id)
            assert status["status"] == "running"
            assert status["alive"] is True
            assert _pid_exists(child_pid)

        stopped = manager.stop(service_id)
        assert stopped["stopped"] is True
        deadline = time.monotonic() + 5
        while _pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _pid_exists(child_pid)
    finally:
        if service_id:
            manager.stop(service_id)
        if child_pid and _pid_exists(child_pid):
            _terminate_windows_process_tree(pid=child_pid, timeout_s=1)


def test_failed_service_returns_sanitized_startup_error(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    manager = _manager(root, state_dir)
    port = _free_tcp_port()

    started = manager.start(
        cmd=_python_cmd(
            "import sys; print('image pull unauthorized', file=sys.stderr); sys.exit(2)"
        ),
        cwd=root,
        readiness={"type": "tcp", "host": "127.0.0.1", "port": port, "timeout_s": 1},
    )

    try:
        assert started.payload["failure_category"] == "readiness_failed"
        assert started.payload["startup_error"] == "image pull unauthorized"
    finally:
        manager.stop(started.service_id)


def test_workspace_preview_serves_loopback_without_docker(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    (root / "index.html").write_bytes(b"preview-ok\n")
    manager = DurableServiceManager(
        root=root,
        state_dir=state_dir,
        settings=ShellSandboxSettings(
            mode="strict",
            backend="docker",
            docker_image="private.invalid/sandbox:dev",
        ),
    )
    service_id = ""
    try:
        started = manager.start_preview(cwd=root)
        service_id = started.service_id

        assert started.payload["status"] == "running"
        assert started.payload["backend"] == "host-preview"
        assert started.payload["preview_access"] == "local"
        assert started.payload["preview_port"] > 0
        assert started.payload["access_url"] == started.payload["preview_url"]
        with urllib.request.urlopen(started.payload["access_url"], timeout=2) as response:
            assert response.status == 200
            assert response.read() == b"preview-ok\n"
    finally:
        if service_id:
            manager.stop(service_id)


def test_workspace_preview_rejects_symlink_escape_and_directory_listing(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"do-not-serve\n")
    try:
        (root / "escape.txt").symlink_to(outside)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise
    (root / ".env").write_text("secret=value\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "escaped-index").mkdir()
    (root / "escaped-index" / "index.html").symlink_to(outside)
    port = _free_tcp_port()
    manager = _manager(root, state_dir)
    service_id = ""
    try:
        started = manager.start_preview(cwd=root, port=port)
        service_id = started.service_id
        base = str(started.payload["preview_url"])

        with pytest.raises(urllib.error.HTTPError) as escape_error:
            urllib.request.urlopen(f"{base}/escape.txt", timeout=2)
        assert escape_error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as listing_error:
            urllib.request.urlopen(f"{base}/nested/", timeout=2)
        assert listing_error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as hidden_error:
            urllib.request.urlopen(f"{base}/.env", timeout=2)
        assert hidden_error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as index_error:
            urllib.request.urlopen(f"{base}/escaped-index/", timeout=2)
        assert index_error.value.code == 403
    finally:
        if service_id:
            manager.stop(service_id)


def test_lan_preview_is_dynamically_addressed_and_temporarily_authenticated(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    (root / "index.html").write_bytes(b"lan-preview-ok\n")
    manager = _manager(root, state_dir)
    service_id = ""
    try:
        started = manager.start_preview(cwd=root, access="lan")
        service_id = started.service_id
        payload = started.payload

        assert payload["preview_access"] == "lan"
        assert payload["authentication_required"] is True
        assert payload["preview_port"] > 0
        assert "alysis_token=" not in payload["preview_url"]
        assert "alysis_token=" in payload["access_url"]
        token = parse_qs(urlsplit(payload["access_url"]).query)["alysis_token"][0]
        metadata_text = (state_dir / service_id / SERVICE_METADATA).read_text(encoding="utf-8")
        assert token not in metadata_text

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        with pytest.raises(urllib.error.HTTPError) as unauthenticated:
            opener.open(payload["preview_url"], timeout=2)
        assert unauthenticated.value.code == 401
        with opener.open(payload["access_url"], timeout=2) as response:
            assert response.status == 200
            assert response.read() == b"lan-preview-ok\n"
            assert "alysis_token=" not in response.geturl()
        service_dir = state_dir / service_id
        assert token not in (service_dir / "stdout.log").read_text(encoding="utf-8")
        assert token not in (service_dir / "stderr.log").read_text(encoding="utf-8")

        fresh = _manager(root, state_dir)
        fresh_status = fresh.status(service_id)
        assert fresh_status["access_url"] == payload["access_url"]
        stopped = fresh.stop(service_id)
        assert stopped["stopped"] is True
        _wait_loaded_popen(manager, service_id)
        assert not (state_dir / service_id / SERVICE_PREVIEW_TOKEN).exists()
    finally:
        if service_id:
            manager.stop(service_id)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_unix_socket_readiness(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    # Darwin limits AF_UNIX paths to roughly 104 bytes; pytest's temp root can
    # exceed that before the socket filename is appended.
    socket_path = Path("/tmp") / f"alysis-{os.getpid()}-{tmp_path.name[-12:]}.sock"
    manager = _manager(root, state_dir)
    service_id = ""

    code = f"""
import os
import socket
import time
path = {str(socket_path)!r}
try:
    os.unlink(path)
except FileNotFoundError:
    pass
server = socket.socket(socket.AF_UNIX)
server.bind(path)
server.listen(1)
time.sleep(30)
"""
    try:
        started = manager.start(
            cmd=_python_cmd(code),
            cwd=root,
            readiness={"type": "unix_socket", "path": str(socket_path), "timeout_s": 5},
        )
        service_id = started.service_id

        assert started.payload["status"] == "running"
        assert started.payload["readiness"]["status"] == "ready"
        assert started.payload["readiness"]["path"] == str(socket_path)
    finally:
        if service_id:
            manager.stop(service_id)
        socket_path.unlink(missing_ok=True)


def test_metadata_excludes_secret_env_and_command_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    root.mkdir()
    secret = "ROUND2_DURABLE_SERVICE_SECRET"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    manager = _manager(root, state_dir)
    service_id = ""

    try:
        started = manager.start(
            cmd=_python_cmd(f"import time; print({secret!r}, flush=True); time.sleep(30)"),
            cwd=root,
            readiness={"type": "process_alive", "timeout_s": 1},
        )
        service_id = started.service_id
        metadata_text = (state_dir / service_id / SERVICE_METADATA).read_text(encoding="utf-8")

        assert secret not in metadata_text
        assert "OPENAI_API_KEY" not in metadata_text
        assert "time.sleep" not in metadata_text
        assert "cmd_sha256" in metadata_text
    finally:
        if service_id:
            manager.stop(service_id)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux procfs required")
def test_stale_pid_metadata_does_not_kill_unrelated_process(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    state_dir = tmp_path / "sessions" / "durable_services"
    service_dir = state_dir / "svc_fake"
    root.mkdir()
    service_dir.mkdir(parents=True)
    pgid = os.getpgid(os.getpid()) if hasattr(os, "getpgid") else None
    (service_dir / SERVICE_METADATA).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_id": "svc_fake",
                "ownership": "DURABLE_SERVICE",
                "status": "running",
                "pid": os.getpid(),
                "pgid": pgid,
                "pid_start_token": "linux:not-the-current-process",
                "started_at_wall": 0,
                "root": str(root),
                "cwd": str(root),
                "backend": "host",
                "cmd_sha256": "0" * 64,
                "readiness": {"type": "process_alive", "timeout_s": 1},
                "stdout_log_path": str(service_dir / "stdout.log"),
                "stderr_log_path": str(service_dir / "stderr.log"),
            }
        ),
        encoding="utf-8",
    )

    payload = _manager(root, state_dir).stop("svc_fake")

    assert payload["stopped"] is False
    assert payload["failure_category"] == "stale_metadata_identity_mismatch"
