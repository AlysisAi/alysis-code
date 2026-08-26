from __future__ import annotations

import json
import os
import shutil
import site
import subprocess
import sys
import textwrap
import tomllib
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

import pytest

from alysis_code import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_MODEL = "gpt-4o-mini"


@dataclass(frozen=True)
class _InstalledCli:
    install_mode: str
    venv_dir: Path
    python: Path
    entrypoint: Path
    wheel_path: Path | None = None


def _venv_python(venv_dir: Path) -> Path:
    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    return scripts_dir / ("python.exe" if os.name == "nt" else "python")


def _venv_entrypoint(venv_dir: Path) -> Path:
    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    candidates = (
        scripts_dir / "alysis.exe",
        scripts_dir / "alysis",
        scripts_dir / "alysis-script.py",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AssertionError(f"Installed alysis entrypoint not found under {scripts_dir}")


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONNOUSERSITE", "1")
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("TERM", "dumb")
    if extra:
        env.update(extra)
    return env


def _runtime_dependency_pythonpath() -> str:
    candidates: list[str] = []
    for raw in [*sys.path, *site.getsitepackages(), site.getusersitepackages()]:
        if not raw:
            continue
        text = str(raw).strip()
        if not text:
            continue
        lowered = text.replace("\\", "/").lower()
        if "site-packages" not in lowered and "dist-packages" not in lowered:
            continue
        if text not in candidates:
            candidates.append(text)
    return os.pathsep.join(candidates)


def _build_backend_env() -> dict[str, str]:
    env = _subprocess_env()
    spec = find_spec("hatchling")
    if spec is not None and spec.submodule_search_locations:
        [backend_package_dir, *_] = list(spec.submodule_search_locations)
        backend_site_packages = os.fspath(Path(backend_package_dir).parent)
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            os.pathsep.join([backend_site_packages, existing_pythonpath])
            if existing_pythonpath
            else backend_site_packages
        )
        return env
    candidate_dirs = [
        REPO_ROOT / ".venv" / "Lib" / "site-packages",
        *sorted((REPO_ROOT / ".venv" / "lib").glob("python*/site-packages")),
    ]
    for candidate in candidate_dirs:
        if (candidate / "hatchling").exists():
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                os.pathsep.join([os.fspath(candidate), existing_pythonpath])
                if existing_pythonpath
                else os.fspath(candidate)
            )
            return env
    pytest.skip("release-smoke packaging tests require hatchling or a repo-local build backend")


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=os.fspath(cwd),
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _assert_completed_ok(proc: subprocess.CompletedProcess[str], *, label: str) -> None:
    assert proc.returncode == 0, (
        f"{label} failed with exit code {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )


def _uv_executable() -> str:
    executable = shutil.which("uv")
    if executable is None:
        pytest.skip("release-smoke packaging tests require uv")
    return executable


def test_editable_build_helper_is_exactly_pinned_and_locked() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "editables==0.5" in project["build-system"]["requires"]
    assert "editables==0.5" in project["project"]["optional-dependencies"]["dev"]

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_versions = {
        str(package.get("version") or "")
        for package in lock.get("package", [])
        if package.get("name") == "editables"
    }
    assert locked_versions == {"0.5"}


def _install_cli(tmp_root: Path, *, install_mode: str) -> _InstalledCli:
    build_env = _build_backend_env()
    install_env = _subprocess_env()
    uv = _uv_executable()
    venv_dir = tmp_root / f"venv-{install_mode}"
    create_proc = _run_subprocess(
        [sys.executable, "-m", "venv", "--system-site-packages", os.fspath(venv_dir)],
        cwd=REPO_ROOT,
        env=install_env,
    )
    _assert_completed_ok(create_proc, label=f"create venv ({install_mode})")

    python = _venv_python(venv_dir)
    wheel_path: Path | None = None
    install_target: list[str]
    if install_mode == "wheel":
        wheelhouse = tmp_root / "wheelhouse"
        wheelhouse.mkdir(parents=True, exist_ok=True)
        wheel_proc = _run_subprocess(
            [
                uv,
                "build",
                "--wheel",
                "--no-build-isolation",
                "--no-sources",
                "--no-create-gitignore",
                "--out-dir",
                os.fspath(wheelhouse),
                os.fspath(REPO_ROOT),
            ],
            cwd=REPO_ROOT,
            env=build_env,
        )
        _assert_completed_ok(wheel_proc, label="build wheel")
        [wheel_path] = sorted(wheelhouse.glob("alysis_code-*.whl"))
        install_target = [os.fspath(wheel_path)]
    elif install_mode == "editable":
        install_target = ["-e", os.fspath(REPO_ROOT)]
    else:
        raise AssertionError(f"Unexpected install mode: {install_mode}")

    install_cmd = [
        uv,
        "pip",
        "install",
        "--python",
        os.fspath(python),
    ]
    if install_mode == "wheel":
        install_cmd.append("--no-deps")
    else:
        install_cmd.extend(["--no-deps", "--no-build-isolation"])
    install_cmd.extend(install_target)

    install_proc = _run_subprocess(
        install_cmd,
        cwd=REPO_ROOT,
        env=build_env if install_mode == "editable" else install_env,
    )
    _assert_completed_ok(install_proc, label=f"install package ({install_mode})")
    return _InstalledCli(
        install_mode=install_mode,
        venv_dir=venv_dir,
        python=python,
        entrypoint=_venv_entrypoint(venv_dir),
        wheel_path=wheel_path,
    )


@pytest.fixture(scope="session")
def installed_wheel_cli(tmp_path_factory: pytest.TempPathFactory) -> _InstalledCli:
    return _install_cli(tmp_path_factory.mktemp("release-smoke-wheel"), install_mode="wheel")


@pytest.fixture(scope="session")
def installed_editable_cli(tmp_path_factory: pytest.TempPathFactory) -> _InstalledCli:
    return _install_cli(tmp_path_factory.mktemp("release-smoke-editable"), install_mode="editable")


@pytest.fixture
def installed_cli(request: pytest.FixtureRequest) -> _InstalledCli:
    return request.getfixturevalue(str(request.param))


def _smoke_runtime_env(state_root: Path) -> dict[str, str]:
    config_dir = state_root / "config"
    data_dir = state_root / "data"
    env = _subprocess_env(
        {
            "ALYSIS_CONFIG_DIR": os.fspath(config_dir),
            "ALYSIS_DATA_DIR": os.fspath(data_dir),
            "ALYSIS_FORGE_PLAN_ASSISTANT": "0",
        }
    )
    runtime_pythonpath = _runtime_dependency_pythonpath()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([runtime_pythonpath, existing_pythonpath])
        if existing_pythonpath
        else runtime_pythonpath
    )
    return env


@pytest.mark.parametrize(
    "installed_cli",
    ["installed_wheel_cli", "installed_editable_cli"],
    indirect=True,
)
def test_installed_cli_entrypoint_smoke_from_outside_source_tree(
    installed_cli: _InstalledCli,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("release smoke workspace\n", encoding="utf-8")
    runtime_env = _smoke_runtime_env(tmp_path / "state")

    help_proc = _run_subprocess(
        [os.fspath(installed_cli.entrypoint), "--help"],
        cwd=workspace,
        env=runtime_env,
    )
    _assert_completed_ok(help_proc, label="installed --help")
    help_text = help_proc.stdout + help_proc.stderr
    assert "Usage:" in help_text
    assert "chat" in help_text
    assert "forge" in help_text

    version_proc = _run_subprocess(
        [os.fspath(installed_cli.entrypoint), "--version"],
        cwd=workspace,
        env=runtime_env,
    )
    _assert_completed_ok(version_proc, label="installed --version")
    # Version first, then commit/timestamp/dirty on the same line. An installed
    # wheel that was not stamped by scripts/generate_build_info.py reports the
    # dev default here, which is the honest answer and is exactly what
    # --require-clean-build refuses.
    printed_version = version_proc.stdout.strip()
    assert printed_version.split()[0] == __version__
    for marker in ("commit:", "built:", "dirty:"):
        assert marker in printed_version

    config_set_proc = _run_subprocess(
        [os.fspath(installed_cli.entrypoint), "config", "set", "model", SMOKE_MODEL],
        cwd=workspace,
        env=runtime_env,
    )
    _assert_completed_ok(config_set_proc, label="installed config set model")

    config_show_proc = _run_subprocess(
        [os.fspath(installed_cli.entrypoint), "config", "show"],
        cwd=workspace,
        env=runtime_env,
    )
    _assert_completed_ok(config_show_proc, label="installed config show")
    assert SMOKE_MODEL in (config_show_proc.stdout + config_show_proc.stderr)

    tools_proc = _run_subprocess(
        [os.fspath(installed_cli.entrypoint), "tools"],
        cwd=workspace,
        env=runtime_env,
    )
    _assert_completed_ok(tools_proc, label="installed tools")
    tools_text = tools_proc.stdout + tools_proc.stderr
    assert "fs_read" in tools_text
    assert "verify_run" in tools_text

    forge_help_proc = _run_subprocess(
        [os.fspath(installed_cli.entrypoint), "forge", "--help"],
        cwd=workspace,
        env=runtime_env,
    )
    _assert_completed_ok(forge_help_proc, label="installed forge --help")
    assert "exec" in (forge_help_proc.stdout + forge_help_proc.stderr)

    config_payload = json.loads(
        (Path(runtime_env["ALYSIS_CONFIG_DIR"]) / "config.json").read_text(encoding="utf-8")
    )
    assert config_payload["model"] == SMOKE_MODEL


def test_installed_wheel_cli_release_smoke_forge_journey_writes_expected_artifacts(
    installed_wheel_cli: _InstalledCli,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# release smoke\n", encoding="utf-8")

    runtime_env = _smoke_runtime_env(tmp_path / "state")
    config_set_proc = _run_subprocess(
        [os.fspath(installed_wheel_cli.entrypoint), "config", "set", "model", SMOKE_MODEL],
        cwd=workspace,
        env=runtime_env,
    )
    _assert_completed_ok(config_set_proc, label="pre-smoke config set")

    result_json = tmp_path / "release_smoke_result.json"
    harness_script = tmp_path / "release_smoke.py"
    harness_script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import json
            import os
            from pathlib import Path

            from typer.testing import CliRunner

            from alysis_code import cli as cli_mod
            from alysis_code.cli import app
            from alysis_code.config import AppConfig
            from alysis_code.forge import load_plan, save_plan

            workspace = Path(os.environ["ALYSIS_SMOKE_WORKSPACE"])
            result_path = Path(os.environ["ALYSIS_SMOKE_RESULT_JSON"])
            calls: list[dict[str, object]] = []

            cli_mod._prompt_forge_entry_plan_assistant = lambda *, console: False

            def fake_run_swarm(**kwargs):
                calls.append(
                    {
                        "mode": kwargs.get("mode"),
                        "scope_mode": kwargs.get("scope_mode"),
                        "verify_mode": kwargs.get("verify_mode"),
                        "parallel": kwargs.get("parallel"),
                        "no_log": kwargs.get("no_log"),
                    }
                )
                paths = kwargs["paths"]
                plan = load_plan(paths)
                for task in plan.get("tasks") or []:
                    task["status"] = "done"
                save_plan(paths, plan)
                (paths.execution_dir / "trace").mkdir(parents=True, exist_ok=True)
                (paths.execution_dir / "worker_results").mkdir(parents=True, exist_ok=True)
                (paths.execution_dir / "swarm_summary.md").write_text(
                    "# Swarm Summary\\n\\n- Release smoke completed successfully.\\n",
                    encoding="utf-8",
                )
                (paths.execution_dir / "trace" / "swarm_trace.jsonl").write_text(
                    '{"phase":"worker.lifecycle","message":"release smoke"}\\n',
                    encoding="utf-8",
                )
                (paths.execution_dir / "worker_results" / "T01.json").write_text(
                    json.dumps({"task_id": "T01", "status": "done"}, indent=2) + "\\n",
                    encoding="utf-8",
                )
                return 0

            cli_mod.run_swarm = fake_run_swarm

            env = {
                "ALYSIS_CONFIG_DIR": os.environ["ALYSIS_CONFIG_DIR"],
                "ALYSIS_DATA_DIR": os.environ["ALYSIS_DATA_DIR"],
                "ALYSIS_FORGE_PLAN_ASSISTANT": "0",
            }
            result = CliRunner().invoke(
                    app,
                    ["chat", "--api-key", "k", "--path", os.fspath(workspace)],
                    input=(
                        "/forge\\n"
                        "/goal Validate installed release smoke\\n"
                        "/task Update src/smoke.py with a packaging smoke note\\n"
                        "/execute plan\\n"
                        "/back\\n"
                        "exit\\n"
                    ),
                env=env,
                terminal_width=120,
            )

            current_run_path = workspace / ".alysis" / "current_run.json"
            pointer = json.loads(current_run_path.read_text(encoding="utf-8"))
            run_dir = workspace / ".alysis" / "runs" / pointer["run_id"]
            sessions_dir = Path(os.environ["ALYSIS_DATA_DIR"]) / "sessions"
            plan = load_plan(cli_mod.load_current_run_paths(workspace))

            payload = {
                "exit_code": result.exit_code,
                "output": result.output,
                "swarm_calls": calls,
                "artifacts": {
                    "current_run_exists": current_run_path.exists(),
                    "plan_json_exists": (run_dir / "plan" / "plan.json").exists(),
                    "plan_md_exists": (run_dir / "plan" / "PLAN.md").exists(),
                    "plan_validation_exists": (
                        run_dir / "plan" / "notes" / "plan_validation.md"
                    ).exists(),
                    "swarm_summary_exists": (run_dir / "execution" / "swarm_summary.md").exists(),
                    "swarm_trace_exists": (
                        run_dir / "execution" / "trace" / "swarm_trace.jsonl"
                    ).exists(),
                    "worker_result_exists": (
                        run_dir / "execution" / "worker_results" / "T01.json"
                    ).exists(),
                },
                "session_logs": sorted(path.name for path in sessions_dir.glob("*.jsonl")),
                "task_statuses": [str(task.get("status") or "") for task in plan.get("tasks") or []],
            }
            result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    proc = _run_subprocess(
        [os.fspath(installed_wheel_cli.python), os.fspath(harness_script)],
        cwd=tmp_path,
        env={
            **runtime_env,
            "ALYSIS_SMOKE_WORKSPACE": os.fspath(workspace),
            "ALYSIS_SMOKE_RESULT_JSON": os.fspath(result_json),
        },
    )
    _assert_completed_ok(proc, label="installed wheel release smoke harness")

    payload = json.loads(result_json.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 0
    assert "Execution complete" in payload["output"]
    assert len(payload["swarm_calls"]) == 1
    assert payload["swarm_calls"][0] == {
        "mode": "auto",
        "scope_mode": "strict",
        "verify_mode": "warn",
        "parallel": 2,
        "no_log": False,
    }
    assert payload["artifacts"] == {
        "current_run_exists": True,
        "plan_json_exists": True,
        "plan_md_exists": True,
        "plan_validation_exists": True,
        "swarm_summary_exists": True,
        "swarm_trace_exists": True,
        "worker_result_exists": True,
    }
    assert payload["session_logs"]
    assert payload["task_statuses"] == ["done"]
