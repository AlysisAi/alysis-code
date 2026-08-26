from __future__ import annotations

import errno
import os
import select
import subprocess
from pathlib import Path

import pytest
from _terminal_matrix_harness import load_scenario, panel_present

pytest.importorskip("termios", reason="show-owl PTY matrix requires POSIX terminal support")
pty = pytest.importorskip("pty", reason="show-owl PTY matrix requires the POSIX pty module")

ROOT = Path(__file__).resolve().parents[1]
SHOW_OWL = ROOT / "src" / "alysis_code" / "assets" / "owl" / "show-owl.sh"


def _run_with_pty(command: list[str], *, env: dict[str, str]) -> tuple[int, str, str]:
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
        )
        os.close(slave_fd)
        slave_fd = -1

        chunks: list[bytes] = []
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                chunks.append(chunk)
                continue
            if process.poll() is not None:
                break

        returncode = process.wait(timeout=10)
        stderr = b""
        if process.stderr is not None:
            stderr = process.stderr.read()
        return (
            returncode,
            b"".join(chunks).decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


@pytest.mark.parametrize("scenario_id", range(5, 12))
def test_show_owl_matrix_subset(scenario_id: int) -> None:
    scenario = load_scenario(scenario_id)
    env = os.environ.copy()
    for key in (
        "COLORTERM",
        "COLORFGBG",
        "NO_COLOR",
        "OWL_THEME",
        "ALYSIS_THEME",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
        "TERM",
        "WT_SESSION",
        "WSL_DISTRO_NAME",
        "WSL_INTEROP",
        "CI",
        "ALYSIS_CI",
    ):
        env.pop(key, None)
    env.update({str(key): str(value) for key, value in scenario.get("env", {}).items()})

    returncode, stdout, stderr = _run_with_pty(
        [
            os.fspath(SHOW_OWL),
            "--theme",
            scenario["expected_theme"],
            "--once",
            "--speed",
            "0.001",
            "--no-text",
        ],
        env=env,
    )

    assert returncode == 0, stderr
    assert "\x1b]11;" not in stdout
    assert "0c/0c0c" not in stdout
    assert panel_present(stdout) is bool(scenario["expects_white_panel"])
