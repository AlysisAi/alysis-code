from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.sandbox_doctor import (
    SandboxCheck,
    SandboxDiagnostic,
    SandboxImagePullResult,
    SandboxPullResult,
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
    }


def _diagnostic(
    *,
    ready: bool = False,
    can_pull: bool = False,
    docker_image: str = "image:dev",
    checks: tuple[SandboxCheck, ...] | None = None,
    next_steps: tuple[str, ...] | None = None,
) -> SandboxDiagnostic:
    return SandboxDiagnostic(
        ready=ready,
        status="ready" if ready else "not_ready",
        configured_mode="strict",
        configured_backend="auto",
        selected_backend="docker",
        docker_image=docker_image,
        server_image="image:server",
        checks=checks
        or (
            SandboxCheck("Docker CLI", "ok", "/usr/bin/docker"),
            SandboxCheck("Docker daemon", "failed", "docker.sock missing"),
        ),
        next_steps=next_steps
        or (
            "Docker is installed, but it is not running. Open Docker Desktop or start the Docker service, then run `alysis doctor sandbox`.",
        ),
        can_pull=can_pull,
    )


def test_doctor_sandbox_reports_beginner_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_diagnose_sandbox(_cfg, *, include_smoke: bool, include_server_image: bool):
        assert include_smoke is True
        assert include_server_image is True
        return _diagnostic()

    monkeypatch.setattr(cli_mod, "diagnose_sandbox", fake_diagnose_sandbox)

    result = CliRunner().invoke(
        alysis_app,
        ["doctor", "sandbox", "--env"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "Alysis Code needs a safe runner" in result.output
    assert "Docker is installed, but it is not running" in result.output
    assert "ALYSIS_SHELL_SANDBOX_MODE" in result.output


def test_sandbox_pull_downloads_images_then_runs_smoke_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_pull_sandbox_images(images, *, timeout_s: int):
        calls["images"] = tuple(images)
        calls["timeout_s"] = timeout_s
        return SandboxPullResult(
            ok=True,
            results=(SandboxImagePullResult("image:dev", True, "pulled image:dev"),),
        )

    def fake_diagnose_sandbox(_cfg, *, include_smoke: bool, include_server_image: bool):
        calls["include_smoke"] = include_smoke
        calls["include_server_image"] = include_server_image
        return _diagnostic(
            ready=True,
            checks=(
                SandboxCheck("Docker CLI", "ok", "/usr/bin/docker"),
                SandboxCheck("Docker daemon", "ok", "running"),
                SandboxCheck("sandbox image", "ok", "image:dev"),
                SandboxCheck("sandbox smoke test", "ok", "command executed in sandbox"),
            ),
            next_steps=("Sandbox is ready.",),
        )

    monkeypatch.setattr(cli_mod, "pull_sandbox_images", fake_pull_sandbox_images)
    monkeypatch.setattr(cli_mod, "diagnose_sandbox", fake_diagnose_sandbox)

    result = CliRunner().invoke(
        alysis_app,
        ["sandbox", "pull", "--image", "image:dev", "--no-server", "--timeout", "7"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert calls == {
        "images": ("image:dev",),
        "timeout_s": 7,
        "include_smoke": True,
        "include_server_image": True,
    }
    assert "pulled image:dev" in result.output
    assert "Alysis Code sandbox is ready" in result.output


def test_sandbox_pull_uses_configured_image_when_no_image_is_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_pull_sandbox_images(images, *, timeout_s: int):
        calls["images"] = tuple(images)
        calls["timeout_s"] = timeout_s
        return SandboxPullResult(
            ok=True,
            results=(SandboxImagePullResult("custom:dev", True, "pulled custom:dev"),),
        )

    def fake_diagnose_sandbox(_cfg, *, include_smoke: bool, include_server_image: bool):
        calls["include_smoke"] = include_smoke
        calls["include_server_image"] = include_server_image
        return _diagnostic(
            ready=True,
            docker_image="custom:dev",
            checks=(
                SandboxCheck("Docker CLI", "ok", "/usr/bin/docker"),
                SandboxCheck("Docker daemon", "ok", "running"),
                SandboxCheck("sandbox image", "ok", "custom:dev"),
                SandboxCheck("sandbox smoke test", "ok", "command executed in sandbox"),
            ),
            next_steps=("Sandbox is ready.",),
        )

    monkeypatch.setattr(cli_mod, "pull_sandbox_images", fake_pull_sandbox_images)
    monkeypatch.setattr(cli_mod, "diagnose_sandbox", fake_diagnose_sandbox)
    env = _env(tmp_path)
    env["ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE"] = "custom:dev"

    result = CliRunner().invoke(
        alysis_app,
        ["sandbox", "pull", "--no-server"],
        env=env,
    )

    assert result.exit_code == 0
    assert calls["images"] == ("custom:dev",)
    assert calls["timeout_s"] == 900
    assert "pulled custom:dev" in result.output


def test_setup_sandbox_pulls_when_docker_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_diagnose_sandbox(_cfg, *, include_smoke: bool, include_server_image: bool):
        calls["diagnose"] = (include_smoke, include_server_image)
        return _diagnostic(
            can_pull=True,
            checks=(
                SandboxCheck("Docker CLI", "ok", "/usr/bin/docker"),
                SandboxCheck("Docker daemon", "ok", "running"),
                SandboxCheck("sandbox image", "missing", "image:dev is not downloaded locally."),
            ),
            next_steps=("Run `alysis sandbox pull` to download Alysis Code's safe runner image.",),
        )

    def fake_run_sandbox_pull_command(*, include_server: bool):
        calls["pull"] = include_server

    monkeypatch.setattr(cli_mod, "diagnose_sandbox", fake_diagnose_sandbox)
    monkeypatch.setattr(cli_mod, "_run_sandbox_pull_command", fake_run_sandbox_pull_command)

    result = CliRunner().invoke(
        alysis_app,
        ["setup", "sandbox"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert calls == {"diagnose": (False, True), "pull": True}
    assert "Downloading Alysis Code sandbox images" in result.output


def test_sandbox_setup_no_pull_keeps_actionable_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_diagnose_sandbox(_cfg, *, include_smoke: bool, include_server_image: bool):
        assert include_smoke is False
        assert include_server_image is True
        return _diagnostic(
            can_pull=True,
            checks=(
                SandboxCheck("Docker CLI", "ok", "/usr/bin/docker"),
                SandboxCheck("Docker daemon", "ok", "running"),
                SandboxCheck("sandbox image", "missing", "image:dev is not downloaded locally."),
            ),
            next_steps=("Run `alysis sandbox pull` to download Alysis Code's safe runner image.",),
        )

    monkeypatch.setattr(cli_mod, "diagnose_sandbox", fake_diagnose_sandbox)

    result = CliRunner().invoke(
        alysis_app,
        ["sandbox", "setup", "--no-pull"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "Alysis Code needs a safe runner" in result.output
    assert "alysis sandbox pull" in result.output
