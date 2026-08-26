from __future__ import annotations

import subprocess

import pytest

import alysis_code.sandbox_doctor as sandbox_doctor_mod
from alysis_code.config import AppConfig
from alysis_code.sandbox_doctor import (
    configured_sandbox_images,
    diagnose_sandbox,
    pull_sandbox_images,
)


def _cfg(*, mode: str = "strict", backend: str = "auto") -> AppConfig:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {
        "shell_sandbox": {
            "mode": mode,
            "backend": backend,
            "docker_image": "ghcr.io/example/alysis-sandbox:dev",
        }
    }
    return cfg


def _cp(args: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_diagnose_sandbox_reports_docker_daemon_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        assert args == ["docker", "info"]
        return _cp(args, returncode=1, stderr="Cannot connect to the Docker daemon " + ("x" * 900))

    monkeypatch.setattr(sandbox_doctor_mod.subprocess, "run", fake_run)

    result = diagnose_sandbox(_cfg(), include_smoke=True)

    assert result.ready is False
    assert result.selected_backend == "docker"
    docker_daemon = next(check for check in result.checks if check.name == "Docker daemon")
    assert docker_daemon.status == "failed"
    assert "Cannot connect to the Docker daemon" in docker_daemon.detail
    assert len(docker_daemon.detail) <= 700
    assert "Docker is installed, but it is not running" in result.next_steps[0]


def test_diagnose_sandbox_suggests_pull_when_docker_image_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        if args == ["docker", "info"]:
            return _cp(args, stdout="ok\n")
        if args[:3] == ["docker", "image", "inspect"]:
            return _cp(args, returncode=1, stderr="No such image")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(sandbox_doctor_mod.subprocess, "run", fake_run)

    result = diagnose_sandbox(_cfg(), include_smoke=False, include_server_image=False)

    assert result.ready is False
    assert result.can_pull is True
    assert any(
        check.name == "sandbox image" and check.status == "missing" for check in result.checks
    )
    assert result.next_steps == (
        "Run `alysis sandbox pull` to download Alysis Code's safe runner image.",
    )


def test_configured_sandbox_images_uses_configured_shell_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_SERVER_DOCKER_IMAGE", "server:custom")

    assert configured_sandbox_images(_cfg(), include_server=True) == (
        "ghcr.io/example/alysis-sandbox:dev",
        "server:custom",
    )


def test_diagnose_sandbox_requires_server_image_when_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setenv("ALYSIS_SERVER_DOCKER_IMAGE", "server:custom")
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        if args == ["docker", "info"]:
            return _cp(args, stdout="ok\n")
        if args == ["docker", "image", "inspect", "ghcr.io/example/alysis-sandbox:dev"]:
            return _cp(args, stdout="ok\n")
        if args == ["docker", "image", "inspect", "server:custom"]:
            return _cp(args, returncode=1, stderr="No such image")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(sandbox_doctor_mod.subprocess, "run", fake_run)

    result = diagnose_sandbox(_cfg(), include_smoke=False, include_server_image=True)

    assert result.ready is False
    assert result.can_pull is True
    server_image = next(check for check in result.checks if check.name == "server sandbox image")
    assert server_image.status == "missing"
    assert result.next_steps == (
        "Run `alysis sandbox pull --server` to download Alysis Code's server worker image.",
    )


def test_diagnose_sandbox_no_backend_gives_beginner_next_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_doctor_mod.shutil, "which", lambda _name: None)

    result = diagnose_sandbox(_cfg(), include_smoke=False)

    assert result.ready is False
    assert result.selected_backend is None
    assert "Install Bubblewrap" in result.next_steps[0]
    assert "Docker" in result.next_steps[1]


def test_diagnose_sandbox_explicit_bwrap_reports_linux_only_on_other_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox_doctor_mod.shutil, "which", lambda _name: None)

    result = diagnose_sandbox(_cfg(backend="bwrap"), include_smoke=False)

    assert result.ready is False
    assert result.selected_backend == "bwrap"
    bubblewrap = next(check for check in result.checks if check.name == "bubblewrap")
    assert bubblewrap.status == "skipped"
    assert "only supported on Linux" in bubblewrap.detail
    assert "ALYSIS_SHELL_SANDBOX_BACKEND=docker" in result.next_steps[0]


def test_diagnose_sandbox_mode_off_is_reported_as_disabled() -> None:
    result = diagnose_sandbox(_cfg(mode="off"), include_smoke=True)

    assert result.ready is True
    assert result.status == "disabled"
    assert result.selected_backend is None
    assert result.checks[0].status == "disabled"


def test_pull_sandbox_images_requires_running_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(
        sandbox_doctor_mod.subprocess,
        "run",
        lambda args, **_kwargs: _cp(args, returncode=1, stderr="docker.sock missing"),
    )

    result = pull_sandbox_images(("image:dev",))

    assert result.ok is False
    assert result.results == ()
    assert result.error is not None
    assert "Docker is installed, but it is not running" in result.error


def test_pull_sandbox_images_pulls_each_requested_image(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        if args == ["docker", "info"]:
            return _cp(args, stdout="ok\n")
        if args[:2] == ["docker", "pull"]:
            return _cp(args, stdout=f"pulled {args[2]}\n")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(sandbox_doctor_mod.subprocess, "run", fake_run)

    result = pull_sandbox_images(("image:dev", "image:server"))

    assert result.ok is True
    assert [item.image for item in result.results] == ["image:dev", "image:server"]
    assert ["docker", "pull", "image:dev"] in calls
    assert ["docker", "pull", "image:server"] in calls


def test_pull_sandbox_images_reports_pull_timeout_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        if args == ["docker", "info"]:
            return _cp(args, stdout="ok\n")
        if args == ["docker", "pull", "image:dev"]:
            raise subprocess.TimeoutExpired(args, timeout=3)
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(sandbox_doctor_mod.subprocess, "run", fake_run)

    result = pull_sandbox_images(("image:dev",), timeout_s=3)

    assert result.ok is False
    assert result.results[0].image == "image:dev"
    assert result.results[0].ok is False
    assert "timed out after 3s" in result.results[0].output


def test_detect_bubblewrap_install_plan_apt_with_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_doctor_mod, "_needs_sudo", lambda: True)
    available = {"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"}
    monkeypatch.setattr(sandbox_doctor_mod.shutil, "which", lambda name: available.get(name))

    plan = sandbox_doctor_mod.detect_bubblewrap_install_plan()

    assert plan is not None
    assert plan.manager == "apt-get"
    assert plan.command == ("sudo", "apt-get", "install", "-y", "bubblewrap")
    assert plan.display == "sudo apt-get install -y bubblewrap"


def test_detect_bubblewrap_install_plan_root_drops_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_doctor_mod, "_needs_sudo", lambda: False)
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/sbin/apk" if name == "apk" else None,
    )

    plan = sandbox_doctor_mod.detect_bubblewrap_install_plan()

    assert plan is not None
    assert plan.command == ("apk", "add", "bubblewrap")


def test_detect_bubblewrap_install_plan_none_when_already_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )

    assert sandbox_doctor_mod.detect_bubblewrap_install_plan() is None


def test_detect_bubblewrap_install_plan_none_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox_doctor_mod.shutil, "which", lambda _name: None)

    assert sandbox_doctor_mod.detect_bubblewrap_install_plan() is None


def test_detect_bubblewrap_install_plan_none_when_sudo_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_doctor_mod, "_needs_sudo", lambda: True)
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    assert sandbox_doctor_mod.detect_bubblewrap_install_plan() is None


def test_install_bubblewrap_success(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = sandbox_doctor_mod.BubblewrapInstallPlan(
        manager="apt-get",
        command=("apt-get", "install", "-y", "bubblewrap"),
        display="apt-get install -y bubblewrap",
    )
    ran: list[list[str]] = []

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        ran.append(list(args))
        return _cp(args, returncode=0)

    monkeypatch.setattr(sandbox_doctor_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox_doctor_mod.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )

    result = sandbox_doctor_mod.install_bubblewrap(plan=plan)

    assert result.ok is True
    assert ran == [["apt-get", "install", "-y", "bubblewrap"]]


def test_install_bubblewrap_reports_when_binary_still_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = sandbox_doctor_mod.BubblewrapInstallPlan(
        manager="apt-get",
        command=("apt-get", "install", "-y", "bubblewrap"),
        display="apt-get install -y bubblewrap",
    )
    monkeypatch.setattr(
        sandbox_doctor_mod.subprocess, "run", lambda args, **_kwargs: _cp(args, returncode=0)
    )
    monkeypatch.setattr(sandbox_doctor_mod.shutil, "which", lambda _name: None)

    result = sandbox_doctor_mod.install_bubblewrap(plan=plan)

    assert result.ok is False
    assert "still not on PATH" in result.detail


def test_install_bubblewrap_reports_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = sandbox_doctor_mod.BubblewrapInstallPlan(
        manager="apt-get",
        command=("apt-get", "install", "-y", "bubblewrap"),
        display="apt-get install -y bubblewrap",
    )
    monkeypatch.setattr(
        sandbox_doctor_mod.subprocess, "run", lambda args, **_kwargs: _cp(args, returncode=100)
    )

    result = sandbox_doctor_mod.install_bubblewrap(plan=plan)

    assert result.ok is False
    assert "code 100" in result.detail


def test_install_bubblewrap_without_plan_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_doctor_mod, "detect_bubblewrap_install_plan", lambda: None)

    result = sandbox_doctor_mod.install_bubblewrap()

    assert result.ok is False
    assert "No supported Linux package manager" in result.detail
