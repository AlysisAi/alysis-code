"""Persistent-service support: non-interactive defaults, probes, finalize check.

Two independent reliability problems live here because both are about a command
that outlives -- or stalls -- the tool call that started it.

*Persistence.* A process the agent starts normally shares the agent's process
group, and that group is exactly what gets signalled at turn finalization and
session close. A service started that way is guaranteed to be dead by the time
an external verifier looks for it. Surviving means leaving the group: its own
session, no controlling terminal, stdio on files rather than pipes nobody will
drain.

*Non-interaction.* Nobody is at the keyboard. A command that stops to ask a
question holds its pipe open until the hard kill and spends the rest of the run
answering nothing.

Stdlib only, no package imports: the agent pulls this in from the tool layer and
the turn controller, and the tests load it straight from this file path in a
bare interpreter.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Non-interactive shell defaults
# ---------------------------------------------------------------------------

# The standard opt-outs for the three ecosystems that prompt most often. apt
# asks about config files and service restarts; git asks for credentials on a
# private remote; pip asks before overwriting. Each one is a silent stall.
NON_INTERACTIVE_ENV_DEFAULTS: dict[str, str] = {
    "DEBIAN_FRONTEND": "noninteractive",
    "GIT_TERMINAL_PROMPT": "0",
    "PIP_NO_INPUT": "1",
}


def apply_non_interactive_defaults(env: dict[str, str]) -> dict[str, str]:
    """Fill in the non-interactive defaults ``env`` does not already set.

    Never overrides. A task that deliberately exports ``DEBIAN_FRONTEND=dialog``
    keeps it -- these are defaults for the unconfigured case, not policy.
    """

    merged = dict(env)
    for key, value in NON_INTERACTIVE_ENV_DEFAULTS.items():
        if key not in merged:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Detached spawn
# ---------------------------------------------------------------------------


def persist_spawn_kwargs() -> dict[str, Any]:
    """Popen kwargs that move the child out of the agent's process group.

    This is the whole of persistence. ``start_new_session`` makes the child a
    session and group leader, so a ``killpg`` aimed at the agent's group cannot
    reach it, and it has no controlling terminal to be hung up on. Windows has
    no sessions; a new process group is the closest equivalent and at least
    detaches the child from console Ctrl-C.
    """

    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# ---------------------------------------------------------------------------
# Liveness probes
# ---------------------------------------------------------------------------

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PROBE_TIMEOUT_S = 0.5
_MIN_PORT = 1
_MAX_PORT = 65535


def valid_port(port: object) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and _MIN_PORT <= port <= _MAX_PORT


def probe_tcp_port(
    port: object,
    *,
    host: str = LOOPBACK_HOST,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
) -> bool:
    """True when something accepts a TCP connection on ``port``.

    Deliberately the cheapest possible check: connect, close. It answers the
    only question that matters to a verifier about to curl the port.
    """

    if not valid_port(port):
        return False
    try:
        with socket.create_connection(
            (host, int(port)),  # type: ignore[arg-type]
            timeout=max(0.01, float(timeout_s)),
        ):
            return True
    except OSError:
        return False


def describe_port_probe(port: object, listening: bool) -> str:
    if not valid_port(port):
        return ""
    number = int(port)  # type: ignore[call-overload]
    return f"listening on :{number}" if listening else f"nothing listening on :{number}"


def pid_alive(pid: object) -> bool:
    """True when ``pid`` names a live process.

    ``os.kill(pid, 0)`` is the POSIX existence probe. It is *not* one on
    Windows, where signal 0 still terminates the target, so that platform takes
    the OpenProcess route instead.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            # Alive, just not ours to signal.
            return True
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:  # pragma: no cover - exercised on Windows
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Port inference
# ---------------------------------------------------------------------------

# First match wins, so the explicit forms come before the positional ones. This
# is a convenience heuristic only: an explicit probe_port argument always wins,
# and a miss costs nothing beyond a pid-only liveness line.
_PORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"--port[=\s]+(\d{1,5})"),
    re.compile(r"\bPORT=(\d{1,5})"),
    # docker-style publish: the host port is the one a verifier can reach.
    re.compile(r"\s-p[=\s]+(\d{1,5}):\d{1,5}"),
    re.compile(r"\s-p[=\s]+(\d{1,5})\b"),
    # 0.0.0.0:8000, localhost:3000, :5000
    re.compile(r"(?:\d{1,3}(?:\.\d{1,3}){3}|localhost)?:(\d{1,5})\b"),
    re.compile(r"\bport\s*=\s*(\d{1,5})", re.IGNORECASE),
)


def parse_probe_port(cmd: str) -> int | None:
    """Best-effort port extraction from a service command line."""

    text = str(cmd or "")
    if not text.strip():
        return None
    for pattern in _PORT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        try:
            candidate = int(match.group(1))
        except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
            continue
        if valid_port(candidate):
            return candidate
    return None


def resolve_probe_port(*, requested: object, cmd: str) -> int | None:
    """An explicit ``probe_port`` wins; otherwise infer one from the command."""

    if requested is None or (isinstance(requested, str) and not requested.strip()):
        return parse_probe_port(cmd)
    try:
        candidate = int(requested)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise ValueError("probe_port must be an integer between 1 and 65535") from None
    if isinstance(requested, bool) or not valid_port(candidate):
        raise ValueError("probe_port must be an integer between 1 and 65535")
    return candidate


def readiness_spec_for_port(
    port: int,
    *,
    host: str = LOOPBACK_HOST,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """A readiness spec the durable-service manager already understands."""

    return {"type": "tcp", "host": host, "port": int(port), "timeout_s": float(timeout_s)}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersistentServiceRecord:
    """One persist-mode start, as remembered for the finalization re-check."""

    service_id: str
    command: str
    pid: int
    probe_port: int | None = None

    def summary(self) -> str:
        label = " ".join(str(self.command or "").split()) or self.service_id
        if len(label) > 120:
            label = f"{label[:117]}..."
        if self.probe_port is not None:
            return f"{label} (pid {self.pid}, port {self.probe_port})"
        return f"{label} (pid {self.pid})"


@dataclass(frozen=True)
class LivenessReport:
    pid_alive: bool
    probe_port: int | None = None
    port_listening: bool | None = None

    @property
    def healthy(self) -> bool:
        if not self.pid_alive:
            return False
        if self.probe_port is None:
            return True
        return bool(self.port_listening)

    def describe(self) -> str:
        parts = ["pid alive" if self.pid_alive else "pid not running"]
        if self.probe_port is not None:
            parts.append(describe_port_probe(self.probe_port, bool(self.port_listening)))
        return "; ".join(part for part in parts if part)

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"pid_alive": self.pid_alive, "liveness": self.describe()}
        if self.probe_port is not None:
            payload["probe_port"] = self.probe_port
            payload["port_listening"] = bool(self.port_listening)
        return payload


PidProbe = Callable[[object], bool]
PortProbe = Callable[[object], bool]


def check_service(
    record: PersistentServiceRecord,
    *,
    pid_probe: PidProbe = pid_alive,
    port_probe: PortProbe = probe_tcp_port,
) -> LivenessReport:
    alive = bool(pid_probe(record.pid))
    if record.probe_port is None:
        return LivenessReport(pid_alive=alive)
    # A dead pid cannot be serving; skip the connect and its timeout.
    listening = bool(port_probe(record.probe_port)) if alive else False
    return LivenessReport(pid_alive=alive, probe_port=record.probe_port, port_listening=listening)


class PersistentServiceRegistry:
    """Session-scoped record of persist-mode starts, in start order."""

    def __init__(self) -> None:
        self._records: dict[str, PersistentServiceRecord] = {}

    def register(self, record: PersistentServiceRecord) -> None:
        self._records[record.service_id] = record

    def forget(self, service_id: object) -> None:
        self._records.pop(str(service_id), None)

    def records(self) -> tuple[PersistentServiceRecord, ...]:
        return tuple(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def __bool__(self) -> bool:
        return bool(self._records)


# ---------------------------------------------------------------------------
# Finalization re-check
# ---------------------------------------------------------------------------

# The single model-visible string this module introduces. Kept as one constant
# so it is greppable and cannot drift.
SERVICE_CHECK_NOTICE_TEMPLATE = (
    "Service check: process started as a persistent service is no longer running: "
    "{summary}. Restart it or note why it is not needed."
)


def finalize_service_notice(
    records: Iterable[PersistentServiceRecord],
    *,
    pid_probe: PidProbe = pid_alive,
    port_probe: PortProbe = probe_tcp_port,
) -> str | None:
    """One notice covering every unhealthy service, or None when all are fine.

    Deliberately a single message no matter how many services died: the point is
    to tell the model something needs attention, not to spend the remaining
    budget enumerating.
    """

    unhealthy: list[str] = []
    for record in records:
        report = check_service(record, pid_probe=pid_probe, port_probe=port_probe)
        if not report.healthy:
            unhealthy.append(f"{record.summary()} - {report.describe()}")
    if not unhealthy:
        return None
    return SERVICE_CHECK_NOTICE_TEMPLATE.format(summary="; ".join(unhealthy))
