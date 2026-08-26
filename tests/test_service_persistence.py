"""Tests for persistent-service support: detachment, probes, finalize check.

Runnable two ways:

    python3 tests/test_service_persistence.py     # standalone, stdlib only
    pytest tests/test_service_persistence.py

The module under test is loaded directly from its file path so that importing
it never executes ``alysis_code/__init__`` or any of the package's
dependency-heavy import chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "alysis_code" / "service_persistence.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("_service_persistence", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the module defines dataclasses, and
    # ``dataclasses`` resolves annotations via ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sp = _load_module()


# The group leader. Started in its own session so the test can kill its whole
# process group without touching the test runner. It starts two identical
# sleepers -- one inheriting its process group, one detached via the function
# under test -- and reports all three pids.
_LAUNCHER = """
import importlib.util, os, subprocess, sys, time

spec = importlib.util.spec_from_file_location("_sp", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
attached = subprocess.Popen(sleeper)
detached = subprocess.Popen(sleeper, **mod.persist_spawn_kwargs())

with open(sys.argv[2], "w") as fh:
    fh.write("%d %d %d\\n" % (os.getpid(), attached.pid, detached.pid))
    fh.flush()
    os.fsync(fh.fileno())

time.sleep(30)
"""


def _wait_until(predicate, *, timeout_s: float = 5.0, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


@unittest.skipIf(os.name == "nt", "POSIX process-group semantics")
class TestDetachedSurvivesProcessGroupKill(unittest.TestCase):
    """The defect: a service in the agent's process group dies with the group."""

    def test_detached_child_survives_group_kill_and_attached_child_does_not(self) -> None:
        pids: list[int] = []
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "launcher.py"
            launcher.write_text(_LAUNCHER, encoding="utf-8")
            report = Path(tmp) / "pids.txt"

            leader = subprocess.Popen(
                [sys.executable, str(launcher), str(_MODULE_PATH), str(report)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                self.assertTrue(
                    _wait_until(lambda: report.exists() and report.read_text().strip()),
                    "launcher never reported its pids",
                )
                leader_pid, attached_pid, detached_pid = (
                    int(part) for part in report.read_text().split()
                )
                pids = [leader_pid, attached_pid, detached_pid]

                # All three are running before the kill.
                self.assertTrue(sp.pid_alive(attached_pid))
                self.assertTrue(sp.pid_alive(detached_pid))

                # The leader is a session leader, so its pid is its pgid. This
                # is the same call the reaper and terminal manager make.
                self.assertEqual(os.getpgid(leader_pid), leader_pid)
                os.killpg(leader_pid, signal.SIGKILL)

                self.assertTrue(
                    _wait_until(lambda: not sp.pid_alive(attached_pid)),
                    "a child sharing the killed process group should not survive",
                )
                # The point of the whole PR.
                self.assertTrue(
                    sp.pid_alive(detached_pid),
                    "a detached child must survive the process-group kill",
                )
                self.assertNotEqual(os.getpgid(detached_pid), leader_pid)
            finally:
                for pid in pids:
                    _kill_quietly(pid)
                _kill_quietly(leader.pid)
                try:
                    leader.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    pass
                if leader.stderr is not None:
                    leader.stderr.close()

    def test_persist_spawn_kwargs_requests_a_new_session(self) -> None:
        self.assertEqual(sp.persist_spawn_kwargs(), {"start_new_session": True})


class TestTcpProbe(unittest.TestCase):
    def test_probe_finds_a_real_listener(self) -> None:
        server = socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(sp.probe_tcp_port(port))
            self.assertEqual(sp.describe_port_probe(port, True), f"listening on :{port}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_probe_reports_a_closed_port(self) -> None:
        # Bind then release: the port is known-good and known-free.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertFalse(sp.probe_tcp_port(port, timeout_s=0.25))
        self.assertEqual(sp.describe_port_probe(port, False), f"nothing listening on :{port}")

    def test_probe_rejects_out_of_range_ports(self) -> None:
        for bad in (0, -1, 65536, None, True, "5000"):
            self.assertFalse(sp.probe_tcp_port(bad))

    def test_pid_alive_tracks_a_real_process(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(sp.pid_alive(proc.pid))
        finally:
            proc.kill()
            proc.wait(timeout=5)
        self.assertFalse(sp.pid_alive(-1))
        self.assertFalse(sp.pid_alive(0))
        self.assertFalse(sp.pid_alive("123"))


class TestPortInference(unittest.TestCase):
    def test_parses_common_service_command_shapes(self) -> None:
        cases = {
            "flask run --port 5000": 5000,
            "flask run --port=5000": 5000,
            "python manage.py runserver 0.0.0.0:8000": 8000,
            "uvicorn app:app --host 0.0.0.0 --port 8080": 8080,
            "PORT=3000 node server.js": 3000,
            "docker run -p 8080:80 nginx": 8080,
            "python -c 'app.run(port=5000)'": 5000,
            "http-server -p 4000": 4000,
            "nc -l localhost:9999": 9999,
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(sp.parse_probe_port(cmd), expected)

    def test_returns_none_when_no_port_is_present(self) -> None:
        for cmd in ("", "   ", "python worker.py", "tail -f /var/log/syslog"):
            self.assertIsNone(sp.parse_probe_port(cmd))

    def test_explicit_probe_port_wins_over_inference(self) -> None:
        self.assertEqual(sp.resolve_probe_port(requested=9000, cmd="flask run --port 5000"), 9000)
        self.assertEqual(sp.resolve_probe_port(requested=None, cmd="flask run --port 5000"), 5000)
        self.assertIsNone(sp.resolve_probe_port(requested=None, cmd="python worker.py"))

    def test_invalid_probe_port_is_rejected(self) -> None:
        for bad in (0, 65536, -1, True, "abc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sp.resolve_probe_port(requested=bad, cmd="python app.py")

    def test_readiness_spec_matches_the_manager_contract(self) -> None:
        self.assertEqual(
            sp.readiness_spec_for_port(5000),
            {"type": "tcp", "host": "127.0.0.1", "port": 5000, "timeout_s": 5.0},
        )


class TestNonInteractiveDefaults(unittest.TestCase):
    def test_all_required_defaults_are_present(self) -> None:
        applied = sp.apply_non_interactive_defaults({})
        self.assertEqual(applied["DEBIAN_FRONTEND"], "noninteractive")
        self.assertEqual(applied["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(applied["PIP_NO_INPUT"], "1")

    def test_existing_values_are_never_overridden(self) -> None:
        applied = sp.apply_non_interactive_defaults({"DEBIAN_FRONTEND": "dialog"})
        self.assertEqual(applied["DEBIAN_FRONTEND"], "dialog")
        self.assertEqual(applied["GIT_TERMINAL_PROMPT"], "0")

    def test_unrelated_environment_is_preserved_and_input_is_not_mutated(self) -> None:
        env = {"PATH": "/usr/bin"}
        applied = sp.apply_non_interactive_defaults(env)
        self.assertEqual(applied["PATH"], "/usr/bin")
        self.assertEqual(env, {"PATH": "/usr/bin"})


class TestRegistryAndFinalizeDecision(unittest.TestCase):
    def _record(self, **kwargs):
        defaults = {"service_id": "svc_1", "command": "flask run", "pid": 4242}
        defaults.update(kwargs)
        return sp.PersistentServiceRecord(**defaults)

    def test_registry_keeps_start_order_and_supports_forget(self) -> None:
        registry = sp.PersistentServiceRegistry()
        self.assertFalse(registry)
        registry.register(self._record(service_id="svc_1"))
        registry.register(self._record(service_id="svc_2"))
        self.assertTrue(registry)
        self.assertEqual(len(registry), 2)
        self.assertEqual([r.service_id for r in registry.records()], ["svc_1", "svc_2"])
        registry.forget("svc_1")
        self.assertEqual([r.service_id for r in registry.records()], ["svc_2"])

    def test_summary_includes_pid_and_port(self) -> None:
        record = self._record(command="flask run --port 5000", probe_port=5000)
        self.assertEqual(record.summary(), "flask run --port 5000 (pid 4242, port 5000)")
        self.assertEqual(self._record().summary(), "flask run (pid 4242)")

    def test_live_service_with_listening_port_produces_no_notice(self) -> None:
        records = [self._record(probe_port=5000)]
        notice = sp.finalize_service_notice(
            records,
            pid_probe=lambda _pid: True,
            port_probe=lambda _port: True,
        )
        self.assertIsNone(notice)

    def test_dead_pid_produces_the_notice(self) -> None:
        records = [self._record(command="flask run --port 5000", probe_port=5000)]
        notice = sp.finalize_service_notice(
            records,
            pid_probe=lambda _pid: False,
            port_probe=lambda _port: True,
        )
        self.assertEqual(
            notice,
            "Service check: process started as a persistent service is no longer running: "
            "flask run --port 5000 (pid 4242, port 5000) - pid not running; "
            "nothing listening on :5000. Restart it or note why it is not needed.",
        )

    def test_live_pid_but_dead_port_produces_the_notice(self) -> None:
        records = [self._record(command="flask run --port 5000", probe_port=5000)]
        notice = sp.finalize_service_notice(
            records,
            pid_probe=lambda _pid: True,
            port_probe=lambda _port: False,
        )
        self.assertIsNotNone(notice)
        self.assertIn("pid alive; nothing listening on :5000", notice)

    def test_no_port_declared_means_pid_liveness_is_enough(self) -> None:
        records = [self._record()]
        self.assertIsNone(sp.finalize_service_notice(records, pid_probe=lambda _pid: True))
        self.assertIsNotNone(sp.finalize_service_notice(records, pid_probe=lambda _pid: False))

    def test_one_notice_covers_every_unhealthy_service(self) -> None:
        records = [
            self._record(service_id="svc_1", command="flask run", pid=1),
            self._record(service_id="svc_2", command="nginx", pid=2),
        ]
        notice = sp.finalize_service_notice(records, pid_probe=lambda _pid: False)
        self.assertIsNotNone(notice)
        self.assertEqual(notice.count("Service check:"), 1)
        self.assertIn("flask run (pid 1)", notice)
        self.assertIn("nginx (pid 2)", notice)

    def test_empty_registry_produces_no_notice(self) -> None:
        self.assertIsNone(sp.finalize_service_notice([]))

    def test_dead_pid_skips_the_port_connect(self) -> None:
        calls: list[object] = []

        def _port_probe(port: object) -> bool:
            calls.append(port)
            return True

        report = sp.check_service(
            self._record(probe_port=5000),
            pid_probe=lambda _pid: False,
            port_probe=_port_probe,
        )
        self.assertEqual(calls, [])
        self.assertFalse(report.healthy)

    def test_liveness_payload_shape(self) -> None:
        report = sp.check_service(
            self._record(probe_port=5000),
            pid_probe=lambda _pid: True,
            port_probe=lambda _port: True,
        )
        self.assertEqual(
            report.as_payload(),
            {
                "pid_alive": True,
                "liveness": "pid alive; listening on :5000",
                "probe_port": 5000,
                "port_listening": True,
            },
        )
        bare = sp.check_service(self._record(), pid_probe=lambda _pid: True)
        self.assertEqual(bare.as_payload(), {"pid_alive": True, "liveness": "pid alive"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
