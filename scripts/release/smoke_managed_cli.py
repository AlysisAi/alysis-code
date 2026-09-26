from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Release smoke tests run untrusted frozen artifacts.  Do not inherit the CI
# job environment: it can contain provider credentials, signing material, and
# package-registry tokens that the artifact has no reason to observe.
_SAFE_ENV_KEYS = (
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)


def _smoke_environment(temp_dir: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    env["ALYSIS_CONFIG_DIR"] = str(temp_dir / "config")
    env["ALYSIS_DATA_DIR"] = str(temp_dir / "data")
    return env


def _request(request_id: str, method: str) -> str:
    return json.dumps(
        {
            "protocol_version": "1",
            "id": request_id,
            "method": method,
            "params": {},
        },
        separators=(",", ":"),
    )


def _run(command: list[str], *, env: dict[str, str], input_text: str | None = None) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"managed CLI smoke command failed with exit code {completed.returncode}: "
            f"{completed.stderr[-2000:]}"
        )
    return completed.stdout


def smoke(executable: Path) -> None:
    resolved = executable.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("managed CLI smoke target is not a file")
    with tempfile.TemporaryDirectory(prefix="alysis-managed-cli-smoke-") as temp_dir:
        env = _smoke_environment(Path(temp_dir))

        version = _run([str(resolved), "--version"], env=env).strip()
        if not version:
            raise RuntimeError("managed CLI --version returned no output")

        health_raw = _run([str(resolved), "ide-bridge", "health"], env=env)
        health = json.loads(health_raw)
        if health.get("ok") is not True or health.get("protocol_version") != "1":
            raise RuntimeError(f"managed CLI health failed: {health}")

        protocol_input = "\n".join(
            (
                _request("smoke-init", "initialize"),
                _request("smoke-shutdown", "bridge.shutdown"),
                "",
            )
        )
        protocol_output = _run(
            [str(resolved), "ide-bridge", "--stdio"],
            env=env,
            input_text=protocol_input,
        )
        responses: dict[str, dict[str, Any]] = {}
        for raw_line in protocol_output.splitlines():
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("managed CLI stdio bridge emitted non-JSON output") from exc
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                responses[payload["id"]] = payload
        initialized = responses.get("smoke-init", {}).get("result")
        shutdown = responses.get("smoke-shutdown", {}).get("result")
        if not isinstance(initialized, dict) or initialized.get("protocol_version") != "1":
            raise RuntimeError("managed CLI stdio initialize handshake failed")
        if not isinstance(shutdown, dict) or shutdown.get("status") != "shutting_down":
            raise RuntimeError("managed CLI graceful stdio shutdown failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke a frozen Alysis Code managed CLI artifact.")
    parser.add_argument("executable", type=Path)
    args = parser.parse_args(argv)
    smoke(args.executable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
