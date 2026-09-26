"""Exercise a frozen bridge repeatedly with no provider credentials or network requests."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release.smoke_managed_cli import _smoke_environment  # noqa: E402


def descendant_pids(pid: int) -> list[int]:
    """Include PyInstaller's child; measuring only its launcher understates runtime memory."""
    if os.name != "nt":
        return [pid]
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("exe", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return [pid]
    parents = {}
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            parents[entry.pid] = entry.parent
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    found = {pid}
    while True:
        descendants = {child for child, parent in parents.items() if parent in found}
        if descendants <= found:
            return sorted(found)
        found.update(descendants)


def memory_bytes(pid: int) -> int | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak",
                "working",
                "pool_peak",
                "pool",
                "np_peak",
                "np",
                "page",
                "page_peak",
                "private",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
    if not handle:
        return None
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return counters.private
        return None
    finally:
        kernel.CloseHandle(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    total = 0
    with (
        tempfile.TemporaryDirectory(prefix="alysis-runtime-soak-") as tmp,
        args.output.open("w", encoding="utf-8") as report,
    ):
        env = _smoke_environment(Path(tmp))
        if os.name == "nt":
            env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
        process = subprocess.Popen(
            [str(executable), "ide-bridge", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=env,
        )
        replies: queue.Queue[str] = queue.Queue(maxsize=128)

        def reader() -> None:
            assert process.stdout
            for line in process.stdout:
                replies.put(line)
            replies.put("")

        threading.Thread(target=reader, daemon=True).start()
        samples: list[float] = []

        def request(method: str, *, expect_ok: bool = True) -> dict:
            nonlocal total
            total += 1
            request_id = f"soak-{total}"
            before = time.monotonic()
            assert process.stdin
            process.stdin.write(
                json.dumps(
                    {"protocol_version": "1", "id": request_id, "method": method, "params": {}}
                )
                + "\n"
            )
            process.stdin.flush()
            raw = replies.get(timeout=30)
            if not raw:
                raise RuntimeError("Bridge exited before replying")
            result = json.loads(raw)
            if result.get("id") != request_id or result.get("ok") is not expect_ok:
                raise RuntimeError(f"Unexpected response identity/status for {method}")
            samples.append((time.monotonic() - before) * 1000)
            return result

        def emit(event: str) -> None:
            ordered = sorted(samples)
            value = {
                "event": event,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "requests": total,
                "process_tree_private_bytes": sum(
                    memory_bytes(pid) or 0 for pid in descendant_pids(process.pid)
                )
                if os.name == "nt"
                else None,
                "latency_p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3)
                if ordered
                else None,
                "latency_max_ms": round(max(samples), 3) if samples else None,
                "pid": process.pid,
                "credential_environment": "isolated",
                "provider_calls": 0,
            }
            line = json.dumps(value)
            report.write(line + "\n")
            report.flush()
            print(line, flush=True)
            samples.clear()

        try:
            first = request("initialize")
            if first["result"].get("protocol_version") != "1":
                raise RuntimeError("Protocol mismatch")
            emit("started")
            next_report = time.monotonic() + 60
            while time.monotonic() - started < args.seconds:
                for method in ("health", "getCapabilities", "qa.unknownMethod"):
                    request(method, expect_ok=method != "qa.unknownMethod")
                if time.monotonic() >= next_report:
                    emit("sample")
                    next_report += 60
                time.sleep(0.25)
            request("bridge.shutdown")
            process.wait(timeout=30)
            if process.returncode != 0:
                raise RuntimeError(f"Unexpected exit code {process.returncode}")
            emit("passed")
            return 0
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
