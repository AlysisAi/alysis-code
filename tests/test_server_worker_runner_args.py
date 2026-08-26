from __future__ import annotations

import os
from pathlib import Path

import pytest

import alysis_code.server.worker_runner as worker_runner_mod
from alysis_code.server.worker_runner import BwrapProcessRunner, DockerProcessRunner


def test_bwrap_build_argv_includes_alysis_job_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    monkeypatch.setattr(worker_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: False)
    monkeypatch.setattr(worker_runner_mod, "_runtime_roots", lambda: [])

    argv = BwrapProcessRunner(network="on").build_argv(
        workspace=workspace,
        job_dir=job_dir,
        argv=["python", "-m", "alysis_code", "run", "--help"],
        env={"HOME": "/tmp", "PATH": "/usr/bin"},
    )

    first_bind = argv.index("--bind")
    second_bind = argv.index("--bind", first_bind + 1)
    assert argv[second_bind + 1] == os.fspath(job_dir.resolve())
    assert argv[second_bind + 2] == "/alysis_job"


def test_docker_build_argv_includes_alysis_job_mount(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    argv = DockerProcessRunner(image="test/alysis-sandbox:server", network="on").build_argv(
        workspace=workspace,
        job_dir=job_dir,
        argv=["python", "-m", "alysis_code", "run", "--help"],
        env={"HOME": "/tmp", "PATH": "/usr/bin"},
    )

    job_mount = f"{os.fspath(job_dir.resolve())}:/alysis_job:rw"
    assert job_mount in argv
