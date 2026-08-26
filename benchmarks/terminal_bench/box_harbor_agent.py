"""
Harbor installed-agent adapter used by the benchmark runner box.

This adapter installs a prebuilt Alysis Code wheel into each Terminal-Bench task
container, then runs ``alysis run`` non-interactively. Keep this file in the
repo so the runner does not depend on an unversioned local rewrite.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
if _SRC_ROOT.exists() and os.fspath(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_SRC_ROOT))

from alysis_code.run_outcome import (  # noqa: E402
    AGENT_FAILURE_EXIT_CODE,
    SUCCESS_EXIT_CODE,
    extract_process_exit_code,
    run_outcome_metadata,
)

try:
    from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext
except ModuleNotFoundError as exc:
    if exc.name and not exc.name.startswith("harbor"):
        raise

    class AgentContext:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.metadata: dict[str, Any] | None = None

    class BaseEnvironment:  # type: ignore[no-redef]
        pass

    def with_prompt_template(fn: Any) -> Any:  # type: ignore[no-redef]
        return fn

    class BaseInstalledAgent:  # type: ignore[no-redef]
        def __init__(
            self,
            logs_dir: Path | str = Path("."),
            model_name: str | None = None,
            version: str | None = None,
            extra_env: dict[str, str] | None = None,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            _ = args, kwargs
            self.logs_dir = Path(logs_dir)
            self.model_name = model_name
            self._version = version
            self._extra_env = dict(extra_env or {})

        def version(self) -> str | None:
            return self._version

        def _get_env(self, key: str) -> str | None:
            if key in self._extra_env:
                return self._extra_env[key]
            return os.environ.get(key)


_WHEEL_DIR = "/tmp/alysis-agent"
_CONFIG_DIR = "/tmp/alysis-cfg"
_ART_DIR = "/logs/artifacts"
_SESSION_DIR = f"{_ART_DIR}/alysis-session"
_CRASH_LOG = f"{_ART_DIR}/alysis-crash.jsonl"
# Terminal-Bench's observed per-task allowance is ~3.5h; 3h leaves the agent a
# margin to finalize inside the harness window instead of being killed by it.
_TBENCH_RUN_BUDGET_SECONDS = "10800"
_SETUP_DIR = "/installed-agent/alysis-source/benchmarks/terminal_bench"
_SETUP_SCRIPT = f"{_SETUP_DIR}/setup.sh"
_SETUP_TIMEOUT_SEC = 1800


class AlysisAgent(BaseInstalledAgent):
    """Installed-agent wrapper around ``alysis run``."""

    SUPPORTS_ATIF = False

    @staticmethod
    def name() -> str:
        return "alysis"

    def version(self) -> str | None:
        return self._get_env("ALYSIS_BENCH_VERSION") or "bench"

    def _host_wheel_path(self) -> str:
        wheel = self._get_env("ALYSIS_WHEEL")
        if not wheel:
            raise RuntimeError(
                "ALYSIS_WHEEL is not set. Point it at the Alysis Code wheel built "
                "from the benchmark branch."
            )
        if not Path(wheel).is_file():
            raise RuntimeError(f"ALYSIS_WHEEL file not found: {wheel}")
        return wheel

    def _host_setup_script_path(self) -> str:
        setup = self._get_env("ALYSIS_TBENCH_SETUP_SH")
        if setup:
            path = Path(setup)
        else:
            path = Path(__file__).with_name("setup.sh")
        if not path.is_file():
            raise RuntimeError(f"Terminal-Bench setup.sh not found: {path}")
        return path.as_posix()

    def _container_wheel_path(self) -> str:
        return f"{_WHEEL_DIR}/{Path(self._host_wheel_path()).name}"

    def _model(self) -> str:
        model = self._get_env("ALYSIS_MODEL")
        if model:
            return model
        mn = getattr(self, "model_name", None)
        if mn:
            return mn.split("/", 1)[-1]
        raise RuntimeError("ALYSIS_MODEL is not set and no model_name was provided.")

    def _base_url(self) -> str:
        base_url = self._get_env("ALYSIS_BASE_URL")
        if not base_url:
            raise RuntimeError(
                "ALYSIS_BASE_URL is not set. Point it at an OpenAI-compatible endpoint."
            )
        return base_url

    def _require_clean_build(self) -> str:
        """Whether this campaign refuses an unidentifiable build (default: yes).

        Opt-out rather than opt-in: the failure this guards against is silent,
        and by the time it is noticed the run is already worthless.
        """
        override = str(self._get_env("ALYSIS_REQUIRE_CLEAN_BUILD") or "").strip()
        return override or "1"

    def _install_env(self) -> dict[str, str]:
        env = {
            "PYTHONUNBUFFERED": "1",
            "ALYSIS_BASE_URL": self._base_url(),
            "ALYSIS_CONFIG_DIR": _CONFIG_DIR,
            "ALYSIS_INSTALL_SPEC": self._get_env("ALYSIS_INSTALL_SPEC") or "alysis-code",
            "ALYSIS_MODEL": self._model(),
            "ALYSIS_MODEL_METADATA_POLICY": "warn",
            # setup.sh ends by running `alysis --version`, whose output now
            # carries the commit and build stamp. Forwarding this makes that
            # probe the campaign's first gate: an unidentifiable build is
            # rejected during install, in the setup log, rather than after a
            # few hundred tasks have already produced unattributable scores.
            "ALYSIS_REQUIRE_CLEAN_BUILD": self._require_clean_build(),
            "ALYSIS_SETUP_ARTIFACT_DIR": f"{_ART_DIR}/setup",
            "ALYSIS_SETUP_LOG_DIR": "/logs/agent/setup",
            "ALYSIS_TBENCH_WEB_SEARCH_MODE": self._get_env("ALYSIS_WEB_SEARCH_MODE") or "off",
            "ALYSIS_VERIFY_SANDBOX_MODE": "off",
            "ALYSIS_WHEEL": self._container_wheel_path(),
        }
        return env

    def _container_env(self) -> dict[str, str]:
        env = {
            "ALYSIS_CONFIG_DIR": _CONFIG_DIR,
            "CI": "1",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "ALYSIS_SHELL_SANDBOX_MODE": self._get_env("ALYSIS_SHELL_SANDBOX_MODE") or "off",
            # Refuse to score a build that cannot say which commit it is. An
            # earlier campaign ran against an unpinned "latest main" and three
            # behaviourally different builds all self-reported "0.9.8", so none
            # of those numbers can now be attributed to a source tree. Defaults
            # on for benchmark runs; set ALYSIS_REQUIRE_CLEAN_BUILD=0 on the
            # host to profile a work-in-progress build deliberately.
            "ALYSIS_REQUIRE_CLEAN_BUILD": self._require_clean_build(),
        }
        api_key = self._get_env("ALYSIS_API_KEY")
        if not api_key:
            raise RuntimeError("ALYSIS_API_KEY is not set on the host.")
        env["ALYSIS_API_KEY"] = api_key
        env["ALYSIS_BASE_URL"] = self._base_url()
        ws_key = self._get_env("ALYSIS_WEB_SEARCH_API_KEY")
        if ws_key:
            env["ALYSIS_WEB_SEARCH_API_KEY"] = ws_key
        llm_timeout = self._get_env("ALYSIS_LLM_TIMEOUT_S")
        if llm_timeout:
            env["ALYSIS_LLM_TIMEOUT_S"] = llm_timeout
        # Run-budget controls, forwarded from the host so a campaign can size
        # the budget to the harness it is actually running under.
        #
        # Harbor does not expose its per-task time allowance to an installed
        # agent -- nothing on BaseInstalledAgent or AgentContext carries it --
        # so it cannot be read here. The CLI's built-in 3600s default is wrong
        # for Terminal-Bench specifically: the observed harness allowance is
        # roughly 3.5h per task, so an unset budget makes the agent stop itself,
        # correctly and cleanly, after using less than a third of the time it
        # was given. In the baseline campaign four tasks did exactly that.
        #
        # This adapter therefore carries a Terminal-Bench-shaped default rather
        # than inheriting the CLI one. The host still wins: set the variable to
        # override, including to a smaller value.
        env["ALYSIS_RUN_BUDGET_SECONDS"] = (
            self._get_env("ALYSIS_RUN_BUDGET_SECONDS") or _TBENCH_RUN_BUDGET_SECONDS
        )
        for budget_var in (
            "ALYSIS_BUDGET_GRACE_SECONDS",
            "ALYSIS_BUDGET_CHECKPOINT_FRACTION",
        ):
            budget_value = self._get_env(budget_var)
            if budget_value:
                env[budget_var] = budget_value
        return env

    def _install_command(self) -> str:
        return (
            f"mkdir -p {shlex.quote(_WHEEL_DIR)} {shlex.quote(_SETUP_DIR)} "
            f"{shlex.quote(_ART_DIR)}/setup /logs/agent/setup "
            f"&& chmod 777 {shlex.quote(_WHEEL_DIR)} "
            f"&& chmod +x {shlex.quote(_SETUP_SCRIPT)} "
            f"&& {shlex.quote(_SETUP_SCRIPT)}"
        )

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(  # type: ignore[attr-defined]
            environment,
            command=(
                f"mkdir -p {shlex.quote(_WHEEL_DIR)} {shlex.quote(_SETUP_DIR)} "
                f"{shlex.quote(_ART_DIR)}/setup /logs/agent/setup "
                f"&& chmod 777 {shlex.quote(_WHEEL_DIR)}"
            ),
        )
        await environment.upload_file(self._host_setup_script_path(), _SETUP_SCRIPT)
        await environment.upload_file(self._host_wheel_path(), self._container_wheel_path())
        await self.exec_as_root(  # type: ignore[attr-defined]
            environment,
            command=self._install_command(),
            env=self._install_env(),
            timeout_sec=_SETUP_TIMEOUT_SEC,
        )

    def _config_set_cmds(self) -> list[str]:
        cmds: list[str] = []
        base_url = self._get_env("ALYSIS_BASE_URL")
        if base_url:
            cmds.append(f"alysis config set base_url {shlex.quote(base_url)}")
        model = self._model()
        if model:
            cmds.append(f"alysis config set model {shlex.quote(model)}")
        ws = {
            "web_search_mode": "ALYSIS_WEB_SEARCH_MODE",
            "web_search_adapter": "ALYSIS_WEB_SEARCH_ADAPTER",
            "web_search_base_url": "ALYSIS_WEB_SEARCH_BASE_URL",
            "web_search_model": "ALYSIS_WEB_SEARCH_MODEL",
            "web_search_timeout_s": "ALYSIS_WEB_SEARCH_TIMEOUT_S",
        }
        for cfg_key, env_name in ws.items():
            val = self._get_env(env_name)
            if val:
                cmds.append(f"alysis config set {cfg_key} {shlex.quote(val)}")
        steps = self._get_env("ALYSIS_MAX_STEPS")
        if steps:
            for cfg_key in ("max_steps", "task_max_steps", "subagent_max_steps"):
                cmds.append(f"alysis config set {cfg_key} {shlex.quote(steps)}")
        cmds.append(f"alysis config set session_log_dir {shlex.quote(_SESSION_DIR)}")
        cmds.append(f"alysis config set crash_diagnostic_log_path {shlex.quote(_CRASH_LOG)}")
        return cmds

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        _ = context
        env = self._container_env()
        model = self._model()
        base_url = env["ALYSIS_BASE_URL"]
        await self.exec_as_agent(  # type: ignore[attr-defined]
            environment,
            command=f"mkdir -p {shlex.quote(_SESSION_DIR)} /logs/agent 2>/dev/null || true",
            env=env,
        )
        await self.exec_as_agent(  # type: ignore[attr-defined]
            environment,
            command=" && ".join(self._config_set_cmds()),
            env=env,
        )

        profile = (self._get_env("ALYSIS_RUN_PROFILE") or "auto").strip().lower()
        parts = [
            "alysis",
            "run",
            "--path",
            ".",
            "--allow-broad-workspace",
            "--yes",
            "--model",
            shlex.quote(model),
            "--base-url",
            shlex.quote(base_url),
            "--api-key-env",
            "ALYSIS_API_KEY",
        ]
        if profile == "benchmark":
            parts.append("--benchmark")
        else:
            parts += ["--mode", shlex.quote(profile)]

        steps = self._get_env("ALYSIS_MAX_STEPS")
        if steps:
            parts += ["--max-steps", shlex.quote(steps)]
        deadline = self._get_env("ALYSIS_DEADLINE_SECONDS")
        if deadline:
            parts += ["--deadline-seconds", shlex.quote(deadline)]
        extra = self._get_env("ALYSIS_EXTRA_ARGS")
        if extra:
            parts.append(extra)
        parts += ["--", shlex.quote(instruction)]

        cmd = (
            "mkdir -p /logs/agent 2>/dev/null; "
            + " ".join(parts)
            + f" </dev/null 2>&1 | tee {_ART_DIR}/alysis.txt /logs/agent/alysis.txt"
        )
        try:
            await self.exec_as_agent(  # type: ignore[attr-defined]
                environment,
                command=cmd,
                env=env,
            )
        except Exception as exc:
            exit_code = extract_process_exit_code(exc)
            context.metadata = {
                **(context.metadata or {}),
                **run_outcome_metadata(
                    exit_code if exit_code is not None else AGENT_FAILURE_EXIT_CODE
                ),
            }
            raise
        else:
            context.metadata = {
                **(context.metadata or {}),
                **run_outcome_metadata(SUCCESS_EXIT_CODE),
            }

    def populate_context_post_run(self, context: AgentContext) -> None:
        _ = context
        return
