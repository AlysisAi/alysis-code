from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ..pipeline_facts import (
    build_pipeline_status_capture_command,
    command_has_top_level_pipe,
    extract_pipeline_status,
    pipeline_meaningful_stage,
    split_top_level_pipeline,
)


class ShellError(RuntimeError):
    pass


_PYTHON_CMD_PATTERN = re.compile(r"(^|(?:&&|\|\||;)\s*)python(?=\s)")


def _python3_fallback_cmd(cmd: str) -> str | None:
    match = _PYTHON_CMD_PATTERN.search(cmd)
    if not match:
        return None
    start, end = match.span()
    prefix = match.group(1)
    replacement = f"{prefix}python3"
    return f"{cmd[:start]}{replacement}{cmd[end:]}"


def _is_python_permission_denied(stderr: str) -> bool:
    lowered = stderr.lower()
    return "python: permission denied" in lowered


def _pipeline_capture_platform_ok() -> bool:
    # PIPESTATUS capture uses a POSIX-shell wrapper; it is unavailable under the
    # Windows cmd.exe shell. (Kept as a separate function so tests can exercise
    # the capture path on any host.)
    return os.name != "nt"


def shell_run(
    *,
    root: Path,
    cmd: str,
    cwd: str | None = None,
    timeout_s: float = 60,
    runner: Any | None = None,
    capture_pipeline_status: bool = False,
) -> dict[str, Any]:
    base = root.resolve()
    if cwd:
        cwd_path = (base / cwd).resolve()
        try:
            cwd_path.relative_to(base)
        except ValueError as e:
            raise ShellError(f"cwd escapes root: {cwd}") from e
    else:
        cwd_path = base

    if runner is None:
        raise ShellError("Shell runner is required; implicit host execution is disabled.")

    # Observe per-stage pipeline exit codes (bash PIPESTATUS) only when it would
    # matter and can be done safely: a top-level pipeline is present, on a POSIX
    # host. The wrapper preserves stdout/stderr/exit-code exactly, so plain
    # commands and non-capturing runs behave identically to before.
    wants_capture = bool(
        capture_pipeline_status
        and _pipeline_capture_platform_ok()
        and pipeline_meaningful_stage(cmd) is not None
    )

    def _run(command_text: str) -> tuple[Any, list[int] | None, str]:
        run_cmd = (
            build_pipeline_status_capture_command(command_text) if wants_capture else command_text
        )
        completed = runner.run(root=base, cwd=cwd_path, cmd=run_cmd, timeout_s=timeout_s)
        if wants_capture:
            status, cleaned_stderr = extract_pipeline_status(completed.stderr or "")
            return completed, status, cleaned_stderr
        return completed, None, (completed.stderr or "")

    try:
        cp, stage_status, stderr_text = _run(cmd)
    except subprocess.TimeoutExpired as e:
        raise ShellError(f"Command timed out after {timeout_s:g}s") from e
    except Exception as e:  # noqa: BLE001
        raise ShellError(f"Failed to run command: {e}") from e

    effective_cmd = cmd
    fallback_cmd = _python3_fallback_cmd(cmd)
    if fallback_cmd and cp.returncode != 0 and _is_python_permission_denied(stderr_text):
        try:
            cp, stage_status, stderr_text = _run(fallback_cmd)
            effective_cmd = fallback_cmd
        except subprocess.TimeoutExpired as e:
            raise ShellError(f"Command timed out after {timeout_s:g}s") from e
        except Exception as e:  # noqa: BLE001
            raise ShellError(f"Failed to run command: {e}") from e

    stdout = cp.stdout or ""
    stderr = stderr_text
    truncated = False
    limit = 20000
    if len(stdout) > limit:
        stdout = stdout[:limit] + "...(truncated)"
        truncated = True
    if len(stderr) > limit:
        stderr = stderr[:limit] + "...(truncated)"
        truncated = True

    # A plain command (no top-level pipe) has a one-element status list equal to
    # its exit code. A pipeline whose per-stage status could not be observed
    # leaves the list as None so the classifier falls back to a single re-run.
    if stage_status is None and not command_has_top_level_pipe(effective_cmd):
        stage_status = [cp.returncode]
    pipeline_stages = split_top_level_pipeline(effective_cmd) or [effective_cmd]

    return {
        "cmd": cmd,
        "effective_cmd": effective_cmd,
        "cwd": str(cwd_path),
        "exit_code": cp.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "pipeline_stages": pipeline_stages,
        "pipeline_stage_status": stage_status,
    }
