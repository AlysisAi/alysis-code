#!/usr/bin/env python3
"""Orchestrate an installed-VSIX acceptance run in a real WSL or Remote-SSH host."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.validate_vscode_remote_acceptance import (  # noqa: E402
    REMOTE_DRIVER_ID,
    REMOTE_ENVIRONMENTS,
    RemoteAcceptanceError,
    candidate_binding,
    inspect_extension_vsix,
    sha256_file,
    validate_evidence,
)

DRIVER_VERSION = "0.0.1"
TRUST_REQUEST_NAME = ".alysis-remote-acceptance-trust-request.json"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PINNED_TRUST_UI_COMMITS = {
    "1.90.0": "89de5a8d4d6205e5b11647eb6a74844ca23d2573",
}
SAFE_AUTHORITY_RE = re.compile(r"(?:wsl|ssh-remote)\+[A-Za-z0-9._-]{1,128}")
SAFE_CONNECTION_RE = re.compile(r"[A-Za-z0-9._@-]{1,256}")
SAFE_POSIX_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,512}")
SAFE_WORKSPACE_ROOT_RE = re.compile(r"/(?:var/)?tmp/alysis-[A-Za-z0-9._/-]{1,384}")


def _fail(message: str) -> None:
    raise RemoteAcceptanceError(message)


def _run(
    argv: list[str],
    *,
    timeout: int = 120,
    input_text: str | None = None,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0 and not allow_failure:
        command = Path(argv[0]).name
        _fail(f"{command} failed with exit code {completed.returncode}.")
    return completed


def _safe_remote_inputs(
    environment: str, authority: str, connection: str, workspace_root: str
) -> None:
    spec = REMOTE_ENVIRONMENTS[environment]
    if SAFE_AUTHORITY_RE.fullmatch(authority) is None or not authority.startswith(
        str(spec["authority_prefix"])
    ):
        _fail(f"Remote authority must be a concrete {spec['authority_prefix']} authority.")
    if SAFE_CONNECTION_RE.fullmatch(connection) is None:
        _fail("Remote connection must be a preconfigured, shell-safe distro name or SSH alias.")
    if (
        SAFE_POSIX_PATH_RE.fullmatch(workspace_root) is None
        or SAFE_WORKSPACE_ROOT_RE.fullmatch(workspace_root) is None
        or "//" in workspace_root
        or any(part in {".", ".."} for part in workspace_root.split("/"))
        or workspace_root in {"/", "/tmp", "/home"}
    ):
        _fail("Remote workspace root must be a narrow absolute POSIX path without traversal.")


class RemoteHost:
    def __init__(self, environment: str, connection: str) -> None:
        self.environment = environment
        self.connection = connection

    def run_script(
        self,
        script: str,
        *args: str,
        timeout: int = 60,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self.environment == "wsl":
            argv = [
                "wsl.exe",
                "--distribution",
                self.connection,
                "--exec",
                "sh",
                "-s",
                "--",
                *args,
            ]
        else:
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=20",
                self.connection,
                "sh",
                "-s",
                "--",
                *args,
            ]
        return _run(
            argv,
            timeout=timeout,
            input_text=script,
            allow_failure=allow_failure,
        )


def _salted_hash(salt: str, value: str) -> str:
    return hashlib.sha256(f"{salt}\0{value}".encode()).hexdigest()


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            _fail("VS Code closed its DevTools connection during the trust action.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _CdpWebSocket:
    """Minimal local-only WebSocket client for two pinned CDP input calls."""

    def __init__(self, endpoint: str, timeout: float) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            _fail("VS Code exposed a non-loopback DevTools endpoint.")
        if parsed.port is None:
            _fail("VS Code DevTools endpoint has no explicit loopback port.")
        self._connection = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self._connection.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        request = (
            f"GET {request_target} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._connection.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) <= 16384:
            response += self._connection.recv(4096)
        header, separator, _remainder = response.partition(b"\r\n\r\n")
        expected_accept = base64.b64encode(
            hashlib.sha1(f"{key}{WEBSOCKET_GUID}".encode("ascii")).digest()
        ).decode("ascii")
        if (
            not separator
            or not header.startswith(b"HTTP/1.1 101 ")
            or f"sec-websocket-accept: {expected_accept}".lower().encode("ascii")
            not in header.lower()
        ):
            self._connection.close()
            _fail("VS Code rejected the loopback DevTools WebSocket handshake.")
        self._next_id = 1

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> _CdpWebSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _send_text(self, payload: bytes) -> None:
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = bytes((0x81, 0x80 | size))
        elif size <= 0xFFFF:
            header = bytes((0x81, 0xFE)) + struct.pack("!H", size)
        else:
            header = bytes((0x81, 0xFF)) + struct.pack("!Q", size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._connection.sendall(header + mask + masked)

    def _receive_text(self) -> str:
        while True:
            first, second = _receive_exact(self._connection, 2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _receive_exact(self._connection, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _receive_exact(self._connection, 8))[0]
            mask = _receive_exact(self._connection, 4) if second & 0x80 else b""
            payload = _receive_exact(self._connection, length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                _fail("VS Code closed DevTools before granting workspace trust.")
            if opcode == 0x9:
                continue
            if opcode != 0x1 or not first & 0x80:
                _fail("VS Code returned an unsupported DevTools WebSocket frame.")
            return payload.decode("utf-8")

    def call(self, method: str, params: dict[str, Any]) -> None:
        call_id = self._next_id
        self._next_id += 1
        self._send_text(
            json.dumps(
                {"id": call_id, "method": method, "params": params},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        while True:
            message = json.loads(self._receive_text())
            if message.get("id") != call_id:
                continue
            if "error" in message:
                _fail(f"VS Code DevTools rejected {method}.")
            return


def grant_workspace_trust_via_cdp(port: int, *, timeout: float = 20.0) -> None:
    """Press the pinned Trust-editor Ctrl+Enter action through loopback CDP."""

    deadline = time.monotonic() + timeout
    endpoint = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            pages = [
                target
                for target in targets
                if isinstance(target, dict)
                and target.get("type") == "page"
                and isinstance(target.get("webSocketDebuggerUrl"), str)
            ]
            if len(pages) == 1:
                endpoint = str(pages[0]["webSocketDebuggerUrl"])
                break
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.25)
    if not endpoint:
        _fail("Could not identify the one isolated VS Code DevTools page.")
    common = {
        "code": "Enter",
        "key": "Enter",
        "modifiers": 2,
        "nativeVirtualKeyCode": 13,
        "windowsVirtualKeyCode": 13,
    }
    with _CdpWebSocket(endpoint, timeout=max(1.0, deadline - time.monotonic())) as client:
        client.call("Input.dispatchKeyEvent", {**common, "type": "rawKeyDown"})
        client.call("Input.dispatchKeyEvent", {**common, "type": "keyUp"})


def stage_driver(compiled_extension: Path, output_dir: Path) -> None:
    if not compiled_extension.is_file() or compiled_extension.is_symlink():
        _fail(f"Compiled remote acceptance driver is missing: {compiled_extension}")
    if output_dir.exists():
        _fail(f"Remote acceptance driver staging directory already exists: {output_dir}")
    output_dir.mkdir(parents=True, mode=0o700)
    shutil.copyfile(compiled_extension, output_dir / "extension.js")
    package = {
        "name": "alysis-remote-acceptance-driver",
        "displayName": "Alysis Code Remote Acceptance Driver",
        "publisher": "alysisai",
        "version": DRIVER_VERSION,
        "private": True,
        "engines": {"vscode": "^1.90.0"},
        "activationEvents": ["onStartupFinished"],
        "main": "./extension.js",
        "extensionKind": ["workspace"],
        "capabilities": {"untrustedWorkspaces": {"supported": True}},
    }
    (output_dir / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


def _assert_driver_vsix(driver_vsix: Path) -> None:
    extension_id, version, target = inspect_extension_vsix(driver_vsix)
    if extension_id != REMOTE_DRIVER_ID or version != DRIVER_VERSION or target is not None:
        _fail("The remote acceptance driver VSIX identity is invalid.")


def _code(
    executable: Path,
    profile_root: Path,
    args: list[str],
    *,
    timeout: int = 180,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            str(executable),
            "--user-data-dir",
            str(profile_root / "user-data"),
            "--extensions-dir",
            str(profile_root / "extensions"),
            *args,
        ],
        timeout=timeout,
        allow_failure=allow_failure,
    )


def run_acceptance(args: argparse.Namespace) -> None:
    if args.environment not in REMOTE_ENVIRONMENTS:
        _fail(f"Unsupported remote environment: {args.environment}")
    _safe_remote_inputs(
        args.environment, args.remote_authority, args.remote_connection, args.workspace_root
    )
    if args.runner_role != REMOTE_ENVIRONMENTS[args.environment]["runner_role"]:
        _fail("Remote acceptance runner role does not match the protected environment.")
    for value, label in (
        (args.candidate_run_id, "candidate_run_id"),
        (args.workflow_run_id, "workflow_run_id"),
        (args.workflow_run_attempt, "workflow_run_attempt"),
    ):
        if value <= 0:
            _fail(f"{label} must be positive.")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.release_tag):
        _fail("release_tag must use vX.Y.Z format.")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        _fail("source_sha must be a full lowercase SHA-1 Git object ID.")
    if not re.fullmatch(r"[0-9a-f]{40}", args.vscode_commit):
        _fail("vscode_commit must be a full lowercase VS Code commit ID.")
    if PINNED_TRUST_UI_COMMITS.get(args.vscode_version) != args.vscode_commit:
        _fail(
            "VS Code version/commit is not approved for the exact-workspace "
            "Trust-editor Ctrl+Enter contract."
        )
    if (
        not args.vscode_executable.is_file()
        or args.vscode_executable.is_symlink()
        or sha256_file(args.vscode_executable) != args.vscode_sha256
    ):
        _fail("Pinned VS Code executable changed after preflight.")
    if (
        not args.remote_extension_vsix.is_file()
        or args.remote_extension_vsix.is_symlink()
        or sha256_file(args.remote_extension_vsix) != args.remote_extension_sha256
    ):
        _fail("Pinned Remote Development prerequisite changed after preflight.")
    vscode_probe = _run([str(args.vscode_executable), "--version"], timeout=30).stdout.splitlines()
    vscode_identity = [line.strip() for line in vscode_probe if line.strip()]
    if (
        len(vscode_identity) < 2
        or vscode_identity[0] != args.vscode_version
        or vscode_identity[1] != args.vscode_commit
    ):
        _fail("VS Code version/commit identity changed after preflight.")
    _assert_driver_vsix(args.driver_vsix)
    prerequisite_id, prerequisite_version, _ = inspect_extension_vsix(args.remote_extension_vsix)
    spec = REMOTE_ENVIRONMENTS[args.environment]
    if (
        prerequisite_id != spec["prerequisite_id"]
        or prerequisite_version != spec["prerequisite_version"]
    ):
        _fail("Pinned Remote Development prerequisite identity changed after preflight.")
    binding = candidate_binding(args.candidate_dir)
    if binding["extension_version"] != args.release_tag.removeprefix("v"):
        _fail("Candidate extension version does not match the release tag.")
    candidate_vsix = Path(binding["vsix_path"])
    acceptance_id = f"{args.workflow_run_id}-{args.workflow_run_attempt}-{args.environment}"
    workspace_path = f"{args.workspace_root.rstrip('/')}/{acceptance_id}"
    if SAFE_POSIX_PATH_RE.fullmatch(workspace_path) is None:
        _fail("Generated remote workspace path is unsafe.")
    identity_salt = secrets.token_hex(32)
    authority_sha = _salted_hash(identity_salt, args.remote_authority)
    workspace_sha = _salted_hash(identity_salt, workspace_path)
    remote = RemoteHost(args.environment, args.remote_connection)
    hostname = remote.run_script(
        'set -eu\ntest "$(uname -s)" = Linux\nhostname\n', timeout=30
    ).stdout.strip()
    if not hostname or "\n" in hostname or "\r" in hostname:
        _fail("Remote Linux host returned an invalid hostname identity.")
    hostname_sha = _salted_hash(identity_salt, hostname)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    config: dict[str, Any] = {
        "schema_name": "vscode-remote-acceptance-driver-config",
        "schema_version": 2,
        "environment": args.environment,
        "release_tag": args.release_tag,
        "source_sha": args.source_sha,
        "candidate_run_id": args.candidate_run_id,
        "workflow_run_id": args.workflow_run_id,
        "workflow_run_attempt": args.workflow_run_attempt,
        "acceptance_id": acceptance_id,
        "started_at": started_at,
        "expected_extension_version": binding["extension_version"],
        "expected_vsix_name": binding["vsix_name"],
        "expected_vsix_sha256": binding["vsix_sha256"],
        "expected_managed_artifact_version": binding["managed_artifact_version"],
        "expected_managed_cli_sha256": binding["managed_cli_sha256"],
        "expected_vscode_version": args.vscode_version,
        "expected_vscode_commit": args.vscode_commit,
        "expected_vscode_executable_sha256": args.vscode_sha256,
        "expected_remote_name": REMOTE_ENVIRONMENTS[args.environment]["remote_name"],
        "expected_runner_role": args.runner_role,
        "expected_prerequisite_id": prerequisite_id,
        "expected_prerequisite_version": prerequisite_version,
        "expected_prerequisite_sha256": args.remote_extension_sha256,
        "identity_salt": identity_salt,
        "expected_authority": args.remote_authority,
        "expected_authority_sha256": authority_sha,
        "expected_hostname_sha256": hostname_sha,
        "expected_workspace_path": workspace_path,
        "expected_workspace_path_sha256": workspace_sha,
    }
    config_b64 = base64.b64encode((json.dumps(config, sort_keys=True) + "\n").encode()).decode()
    settings_b64 = base64.b64encode(
        (
            json.dumps(
                {
                    "alysis.autoStartBridge": False,
                    "alysis.cliPath": "",
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
    ).decode()
    profile_root = Path(
        tempfile.mkdtemp(prefix=f"alysis-{args.environment}-", dir=args.local_temp_root)
    )
    user_settings = profile_root / "user-data" / "User" / "settings.json"
    user_settings.parent.mkdir(parents=True, mode=0o700)
    user_settings.write_text(
        json.dumps(
            {
                "security.workspace.trust.enabled": True,
                "security.workspace.trust.startupPrompt": "never",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        _fail(f"Remote acceptance output already exists: {output_path}")
    remote_install_started = False
    try:
        remote.run_script(
            """set -eu
root=$1
config_b64=$2
settings_b64=$3
test ! -e "$root"
umask 077
mkdir -p "$root/.vscode"
printf '%s' "$config_b64" | base64 -d > "$root/.alysis-remote-acceptance-config.json"
printf '%s' "$settings_b64" | base64 -d > "$root/.vscode/settings.json"
""",
            workspace_path,
            config_b64,
            settings_b64,
        )
        _code(
            args.vscode_executable,
            profile_root,
            ["--install-extension", str(args.remote_extension_vsix.resolve()), "--force"],
        )
        local_inventory = (
            _code(
                args.vscode_executable,
                profile_root,
                ["--list-extensions", "--show-versions"],
            )
            .stdout.lower()
            .splitlines()
        )
        if f"{prerequisite_id}@{prerequisite_version}" not in {
            line.strip() for line in local_inventory
        }:
            _fail("Pinned Remote Development prerequisite was not installed exactly.")

        remote_common = ["--remote", args.remote_authority]
        preinstall_inventory = {
            line.strip().lower()
            for line in _code(
                args.vscode_executable,
                profile_root,
                [*remote_common, "--list-extensions", "--show-versions"],
                timeout=180,
            ).stdout.splitlines()
            if line.strip()
        }
        forbidden_existing = {
            item
            for item in preinstall_inventory
            if item.startswith("alysisai.vscode-alysis@") or item.startswith(f"{REMOTE_DRIVER_ID}@")
        }
        if forbidden_existing:
            _fail(
                "Dedicated remote host is not clean; remove prior Alysis Code acceptance installs."
            )
        remote_install_started = True
        for extension_path in (candidate_vsix, args.driver_vsix.resolve()):
            _code(
                args.vscode_executable,
                profile_root,
                [*remote_common, "--install-extension", str(extension_path), "--force"],
                timeout=300,
            )
        remote_inventory = {
            line.strip().lower()
            for line in _code(
                args.vscode_executable,
                profile_root,
                [*remote_common, "--list-extensions", "--show-versions"],
                timeout=180,
            ).stdout.splitlines()
            if line.strip()
        }
        expected_target = f"alysisai.vscode-alysis@{binding['extension_version']}"
        expected_driver = f"{REMOTE_DRIVER_ID}@{DRIVER_VERSION}"
        if expected_target not in remote_inventory or expected_driver not in remote_inventory:
            _fail(
                "Remote extension inventory does not contain the exact target and driver versions."
            )

        devtools_port = _reserve_loopback_port()
        _code(
            args.vscode_executable,
            profile_root,
            [
                *remote_common,
                workspace_path,
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={devtools_port}",
                "--new-window",
                "--skip-welcome",
                "--skip-release-notes",
            ],
            timeout=60,
        )
        deadline = time.monotonic() + args.timeout_seconds
        report_text = ""
        trust_action_sent = False
        while time.monotonic() < deadline:
            result = remote.run_script(
                """set -eu
result=$1/.alysis-remote-acceptance-result.json
trust=$1/.alysis-remote-acceptance-trust-request.json
if test -f "$result"; then
  printf 'RESULT\n'
  cat "$result"
elif test -f "$trust"; then
  printf 'TRUST\n'
fi
""",
                workspace_path,
                timeout=30,
                allow_failure=True,
            )
            if result.returncode == 0:
                kind, separator, payload = result.stdout.partition("\n")
                if kind == "RESULT" and separator and payload.strip():
                    report_text = payload
                    break
                if kind == "TRUST" and not trust_action_sent:
                    grant_workspace_trust_via_cdp(devtools_port)
                    trust_action_sent = True
            time.sleep(5)
        if not report_text:
            state = (
                "after the Trust-editor action" if trust_action_sent else "before requesting trust"
            )
            _fail(
                f"Remote Extension Host did not produce acceptance evidence {state} before timeout."
            )
        try:
            report = json.loads(report_text)
        except json.JSONDecodeError as exc:
            raise RemoteAcceptanceError(
                "Remote Extension Host produced invalid JSON evidence."
            ) from exc
        if not isinstance(report, dict):
            _fail("Remote Extension Host evidence must be a JSON object.")
        validate_evidence(
            report,
            args.candidate_dir,
            expected_environment=args.environment,
            expected_release_tag=args.release_tag,
            expected_source_sha=args.source_sha,
            expected_candidate_run_id=args.candidate_run_id,
            expected_workflow_run_id=args.workflow_run_id,
            expected_workflow_run_attempt=args.workflow_run_attempt,
            expected_vscode_version=args.vscode_version,
            expected_vscode_commit=args.vscode_commit,
            expected_vscode_executable_sha256=args.vscode_sha256,
            expected_prerequisite_sha256=args.remote_extension_sha256,
            expected_authority_sha256=authority_sha,
            expected_hostname_sha256=hostname_sha,
            expected_workspace_path_sha256=workspace_sha,
        )
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    finally:
        if remote_install_started:
            _code(
                args.vscode_executable,
                profile_root,
                ["--remote", args.remote_authority, "--uninstall-extension", REMOTE_DRIVER_ID],
                timeout=120,
                allow_failure=True,
            )
            _code(
                args.vscode_executable,
                profile_root,
                [
                    "--remote",
                    args.remote_authority,
                    "--uninstall-extension",
                    "alysisai.vscode-alysis",
                ],
                timeout=120,
                allow_failure=True,
            )
        remote.run_script(
            """set -eu
root=$1
case "$root" in
  /tmp/alysis-*/*|/var/tmp/alysis-*/*) rm -rf -- "$root" ;;
  *) exit 64 ;;
esac
""",
            workspace_path,
            timeout=30,
            allow_failure=True,
        )
        shutil.rmtree(profile_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage-driver")
    stage.add_argument("compiled_extension", type=Path)
    stage.add_argument("output_dir", type=Path)
    run = subparsers.add_parser("run")
    run.add_argument("candidate_dir", type=Path)
    run.add_argument("driver_vsix", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--environment", choices=tuple(REMOTE_ENVIRONMENTS), required=True)
    run.add_argument("--runner-role", required=True)
    run.add_argument("--release-tag", required=True)
    run.add_argument("--source-sha", required=True)
    run.add_argument("--candidate-run-id", type=int, required=True)
    run.add_argument("--workflow-run-id", type=int, required=True)
    run.add_argument("--workflow-run-attempt", type=int, required=True)
    run.add_argument("--vscode-executable", type=Path, required=True)
    run.add_argument("--vscode-sha256", required=True)
    run.add_argument("--vscode-version", required=True)
    run.add_argument("--vscode-commit", required=True)
    run.add_argument("--remote-extension-vsix", type=Path, required=True)
    run.add_argument("--remote-extension-sha256", required=True)
    run.add_argument("--remote-authority", required=True)
    run.add_argument("--remote-connection", required=True)
    run.add_argument("--workspace-root", required=True)
    run.add_argument("--local-temp-root", type=Path, default=Path(tempfile.gettempdir()))
    run.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage-driver":
            stage_driver(args.compiled_extension, args.output_dir)
        else:
            if args.timeout_seconds < 60 or args.timeout_seconds > 1800:
                _fail("timeout-seconds must be between 60 and 1800.")
            if not args.local_temp_root.is_dir():
                _fail("local-temp-root must already exist.")
            run_acceptance(args)
    except RemoteAcceptanceError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
