from __future__ import annotations

import socket
import threading

import pytest

import alysis_code.preview_server as preview_server


def test_runtime_environment_detects_wsl_from_interop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preview_server.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preview_server.platform, "release", lambda: "generic-linux")
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

    assert preview_server._runtime_environment() == "wsl"


def test_darwin_lan_discovery_uses_default_route_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preview_server.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        preview_server.shutil,
        "which",
        lambda command: f"/runtime/{command}" if command in {"route", "ipconfig"} else None,
    )

    def fake_run(argv: list[str]) -> str:
        if argv[0].endswith("route"):
            return "route to: default\ninterface: runtime0\n"
        if argv[0].endswith("ipconfig") and argv[-1] == "runtime0":
            return "192.0.2.44\n"
        return ""

    monkeypatch.setattr(preview_server, "_run_discovery_command", fake_run)

    assert preview_server._discover_platform_lan_hosts(socket.AF_INET) == ["192.0.2.44"]


def test_bounded_address_discovery_returns_without_waiting_for_slow_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()

    def slow_getaddrinfo(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        gate.wait(1)
        return []

    monkeypatch.setattr(preview_server.socket, "getaddrinfo", slow_getaddrinfo)

    try:
        assert (
            preview_server._bounded_getaddrinfo(
                "runtime-host",
                None,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
                timeout_s=0.01,
            )
            == []
        )
    finally:
        gate.set()
