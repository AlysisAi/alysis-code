from __future__ import annotations

import ipaddress
import logging
import os
import platform
import shutil
import site
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from .branding import default_sandbox_docker_image
from .bwrap_etc import ensure_minimal_etc_dir
from .config import AppConfig, ConfigError
from .process_reaping import ProcessGroupRegistry, run_in_tracked_process_group
from .sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings
from .service_persistence import apply_non_interactive_defaults

_SENSITIVE_ENV_KEYS = {"ALYSIS_API_KEY", "OPENAI_API_KEY"}
_LOGGER = logging.getLogger("alysis_code.sandbox_runner")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_BWRAP_UNSHARE_CGROUP_SUPPORTED: bool | None = None
_PROTECTED_REPO_META = (".git", ".alysis", ".alysis_images", "alysis-feedback")
_BWRAP_SYSTEM_BIND_ROOTS = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"))
_BWRAP_COMMON_TOOLCHAIN_COMMANDS = (
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "py.test",
    "ruff",
    "mypy",
    "pyright",
    "tox",
    "nox",
    "uv",
    "poetry",
    "pipx",
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "bun",
    "cargo",
    "rustc",
    "rustup",
    "go",
    "java",
    "javac",
    "jar",
    "mvn",
    "gradle",
)


class ShellRunner(Protocol):
    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]: ...


def _sanitized_env(*, clear_env: bool) -> dict[str, str]:
    # Every shell backend's environment funnels through here, which makes it the
    # one place to declare that nobody is at the keyboard.
    if clear_env:
        env: dict[str, str] = {"HOME": "/tmp/home"}
        env["PATH"] = os.environ.get("PATH") or _DEFAULT_PATH
        lang = os.environ.get("LANG")
        if lang:
            env["LANG"] = lang
        return apply_non_interactive_defaults(env)
    blocked = {key.upper() for key in _SENSITIVE_ENV_KEYS}
    return apply_non_interactive_defaults(
        {k: v for k, v in os.environ.items() if k.upper() not in blocked}
    )


def _container_env(clear_env: bool, *, allowlist: tuple[str, ...] = ()) -> dict[str, str]:
    if clear_env:
        env: dict[str, str] = {"HOME": "/tmp/home"}
        lang = os.environ.get("LANG")
        if lang:
            env["LANG"] = lang
        return env
    env = _sanitized_env(clear_env=False)
    blocked = {"PATH", "HOME"}
    filtered = {k: v for k, v in env.items() if k.upper() not in blocked}
    if allowlist:
        allowed = {item.upper() for item in allowlist}
        allowed.add("LANG")
        filtered = {k: v for k, v in filtered.items() if k.upper() in allowed}
    filtered["HOME"] = "/tmp/home"
    return filtered


def _workspace_cwd(*, root: Path, cwd: Path) -> str:
    root_abs = root.resolve()
    cwd_abs = cwd.resolve()
    try:
        rel = cwd_abs.relative_to(root_abs)
    except ValueError as e:
        raise RuntimeError(f"cwd is outside root: {cwd_abs}") from e
    if not rel.parts:
        return "/workspace"
    return os.fspath(PurePosixPath("/workspace", *rel.parts))


def _protected_repo_paths(root: Path) -> list[tuple[Path, str]]:
    root_abs = root.resolve()
    protected: list[tuple[Path, str]] = []
    for rel in _PROTECTED_REPO_META:
        candidate = root_abs / rel
        if not candidate.exists():
            continue
        # Never follow symlinked metadata roots to avoid binding host paths.
        if candidate.is_symlink():
            continue
        host_path = candidate.resolve()
        try:
            host_path.relative_to(root_abs)
        except ValueError:
            continue
        protected.append((host_path, rel))
    return protected


def _path_is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _path_is_inside_system_root(path: Path) -> bool:
    return _path_is_under_any(path, _BWRAP_SYSTEM_BIND_ROOTS)


def _path_is_lexically_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    candidate = path if path.is_absolute() else path.absolute()
    for root in roots:
        root_abs = root if root.is_absolute() else root.absolute()
        try:
            candidate.relative_to(root_abs)
            return True
        except ValueError:
            continue
    return False


def _dedupe_existing_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    roots: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.exists() or _path_is_inside_system_root(resolved):
            continue
        if _path_is_under_any(resolved, tuple(roots)):
            continue
        key = os.fspath(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return tuple(roots)


def _executable_symlink_mounts(executable: Path) -> tuple[tuple[Path, Path], ...]:
    mounts: list[tuple[Path, Path]] = []
    seen: set[str] = set()

    def add_mount(host_path: Path, dest_path: Path) -> None:
        try:
            host_resolved = host_path.resolve()
        except OSError:
            return
        if not host_resolved.exists() or _path_is_inside_system_root(host_resolved):
            return
        key = f"{os.fspath(host_resolved)}\0{dest_path.as_posix()}"
        if key in seen:
            return
        seen.add(key)
        mounts.append((host_resolved, dest_path))

    current = executable
    for _ in range(16):
        try:
            if not current.is_symlink():
                break
            target_raw = os.readlink(current)
        except OSError:
            break
        target = Path(target_raw)
        if not target.is_absolute():
            target = current.parent / target
        if not _path_is_lexically_under_any(target, (Path(sys.prefix), Path(sys.exec_prefix))):
            add_mount(target, target)
        current = target
    return tuple(mounts)


def _executable_symlink_parent_roots(executable: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[str] = set()

    def add_root(path: Path) -> None:
        key = os.fspath(path)
        if key in seen:
            return
        seen.add(key)
        roots.append(path)

    def add_symlink_ancestor_parent_roots(path: Path) -> None:
        current = Path(path.anchor or "/")
        for part in path.parts[1:-1]:
            current = current / part
            try:
                is_link = current.is_symlink()
            except OSError:
                continue
            if is_link:
                add_root(current.parent)

    current = executable
    for _ in range(16):
        try:
            if not current.is_symlink():
                break
            target_raw = os.readlink(current)
        except OSError:
            break
        target = Path(target_raw)
        if not target.is_absolute():
            target = current.parent / target
        add_root(target.parent)
        add_symlink_ancestor_parent_roots(target)
        current = target
    return tuple(roots)


def _python_runtime_ro_bind_roots() -> tuple[Path, ...]:
    executable = Path(sys.executable)
    candidates = _dedupe_existing_paths(
        [
            Path(sys.prefix),
            Path(sys.exec_prefix),
            Path(sys.base_prefix),
            Path(sys.base_exec_prefix),
            executable.resolve().parent,
            *_executable_symlink_parent_roots(executable),
        ]
    )
    return candidates


def _add_bwrap_dir(
    args: list[str],
    dest: PurePosixPath,
    *,
    created_dirs: set[str],
) -> None:
    dest_s = dest.as_posix()
    if dest_s in {"", ".", "/"} or dest.parent == dest or dest_s in created_dirs:
        return
    _add_bwrap_dir(args, dest.parent, created_dirs=created_dirs)
    args.extend(["--dir", dest_s])
    created_dirs.add(dest_s)


def _add_bwrap_ro_bind(
    args: list[str],
    host_path: Path,
    dest_path: Path,
    *,
    created_dirs: set[str],
    bound_dests: set[str],
    resolve_dest: bool = True,
) -> None:
    host_resolved = host_path.resolve()
    if not host_resolved.exists():
        return
    dest = PurePosixPath((dest_path.resolve() if resolve_dest else dest_path.absolute()).as_posix())
    dest_s = dest.as_posix()
    if dest_s in bound_dests:
        return
    _add_bwrap_dir(args, dest.parent, created_dirs=created_dirs)
    args.extend(["--ro-bind", os.fspath(host_resolved), dest_s])
    bound_dests.add(dest_s)


def _add_python_runtime_bindings(args: list[str], *, created_dirs: set[str]) -> None:
    bound_dests: set[str] = set()
    for root in _python_runtime_ro_bind_roots():
        _add_bwrap_ro_bind(
            args,
            root,
            root,
            created_dirs=created_dirs,
            bound_dests=bound_dests,
        )


def _path_env_entries(path_value: str | None) -> tuple[Path, ...]:
    entries: list[Path] = []
    for raw in (path_value or "").split(os.pathsep):
        if not raw.strip():
            continue
        entries.append(Path(raw.strip()))
    return tuple(entries)


def _toolchain_root_for_executable(executable: Path, *, command_name: str) -> Path:
    resolved = executable.resolve()
    parts = tuple(part.casefold() for part in resolved.parts)
    if ".cargo" in parts:
        index = parts.index(".cargo")
        return Path(*resolved.parts[: index + 1])
    if ".rustup" in parts:
        index = parts.index(".rustup")
        return Path(*resolved.parts[: index + 1])
    if command_name in {"node", "npm", "npx", "pnpm", "yarn", "bun"}:
        for marker in (".nvm", ".fnm", ".volta"):
            if marker in parts:
                index = parts.index(marker)
                # Bind the version manager root. npm/yarn shims commonly jump between
                # bin and lib/node_modules under the same versioned install.
                if marker == ".nvm" and index + 3 < len(resolved.parts):
                    return Path(*resolved.parts[: index + 4])
                return Path(*resolved.parts[: index + 1])
    if resolved.parent.name == "bin":
        return resolved.parent.parent
    return resolved.parent


def _toolchain_extra_env(bind_roots: tuple[Path, ...]) -> dict[str, str]:
    env: dict[str, str] = {}
    cargo_home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")
    rustup_home = Path(os.environ.get("RUSTUP_HOME") or Path.home() / ".rustup")
    python_userbase = Path(site.getuserbase())
    if any(root == cargo_home.resolve() for root in bind_roots if root.exists()):
        env["CARGO_HOME"] = os.fspath(cargo_home.resolve())
    if rustup_home.exists():
        env["RUSTUP_HOME"] = os.fspath(rustup_home.resolve())
    if python_userbase.exists() and any(
        root == python_userbase.resolve() for root in bind_roots if root.exists()
    ):
        env["PYTHONUSERBASE"] = os.fspath(python_userbase.resolve())
    return env


def _common_toolchain_ro_bind_roots(path_value: str | None) -> tuple[Path, ...]:
    search_path = os.pathsep.join(os.fspath(path) for path in _path_env_entries(path_value))
    candidates: list[Path] = []
    for command_name in _BWRAP_COMMON_TOOLCHAIN_COMMANDS:
        executable = shutil.which(command_name, path=search_path or None)
        if not executable:
            continue
        executable_path = Path(executable)
        if _path_is_inside_system_root(executable_path):
            continue
        candidates.append(
            _toolchain_root_for_executable(executable_path, command_name=command_name)
        )
        if command_name in {"cargo", "rustc", "rustup"}:
            cargo_home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")
            rustup_home = Path(os.environ.get("RUSTUP_HOME") or Path.home() / ".rustup")
            candidates.extend([cargo_home, rustup_home])
    return _dedupe_existing_paths(candidates)


def _add_common_toolchain_bindings(
    args: list[str],
    *,
    path_value: str | None,
    created_dirs: set[str],
) -> dict[str, str]:
    bound_dests: set[str] = set()
    bind_roots = _common_toolchain_ro_bind_roots(path_value)
    for root in bind_roots:
        _add_bwrap_ro_bind(
            args,
            root,
            root,
            created_dirs=created_dirs,
            bound_dests=bound_dests,
        )
    return _toolchain_extra_env(bind_roots)


def _hardened_java_config_ro_bind_paths(*, etc_root: Path = Path("/etc")) -> tuple[Path, ...]:
    paths: list[Path] = []
    seen: set[str] = set()
    try:
        candidates = [*etc_root.glob("java-*"), etc_root / ".java"]
    except OSError:
        candidates = [etc_root / ".java"]
    for candidate in sorted(candidates, key=os.fspath):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_dir():
            continue
        resolved_key = os.fspath(resolved)
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        paths.append(candidate)
    return tuple(paths)


def _add_hardened_java_config_bindings(args: list[str]) -> None:
    for path in _hardened_java_config_ro_bind_paths():
        # bwrap consumes Linux namespace paths even when this pure argv builder
        # is exercised by the Windows test harness.
        path_s = path.as_posix()
        args.extend(["--ro-bind", path_s, path_s])


def _bwrap_path_value(path_value: str | None) -> str:
    entries: list[str] = []
    for raw in (path_value or _DEFAULT_PATH).split(os.pathsep):
        cleaned = raw.strip()
        if cleaned and cleaned not in entries:
            entries.append(cleaned)
    return os.pathsep.join(entries)


@dataclass(frozen=True)
class HostShellRunner:
    process_group_registry: ProcessGroupRegistry | None = None
    close_stdin: bool = False

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        return run_in_tracked_process_group(
            cmd,
            shell=True,
            cwd=str(cwd),
            timeout=timeout_s,
            registry=self.process_group_registry,
            origin="shell_run:host",
            command_label=cmd,
            stdin=subprocess.DEVNULL if self.close_stdin else None,
        )


@dataclass(frozen=True)
class DisabledShellRunner:
    reason: str = "Shell execution is disabled."

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        raise RuntimeError(self.reason)


class LazyShellRunner:
    def __init__(self, loader: Callable[[], ShellRunner]) -> None:
        self._loader = loader
        self._runner: ShellRunner | None = None
        self._load_error: Exception | None = None

    def _resolve_runner(self) -> ShellRunner:
        if self._runner is not None:
            return self._runner
        if self._load_error is not None:
            raise self._load_error
        try:
            runner = self._loader()
        except Exception as exc:
            self._load_error = exc
            raise
        self._runner = runner
        return runner

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        return self._resolve_runner().run(root=root, cwd=cwd, cmd=cmd, timeout_s=timeout_s)


def _build_bwrap_argv(
    *,
    root: Path,
    cwd: Path,
    cmd: str,
    network: str,
    clear_env: bool,
    profile: str,
    unshare_cgroup: bool,
) -> tuple[list[str], dict[str, str]]:
    """Build bwrap argv + parent-process env. Pure function; no subprocess calls."""

    root_abs = root.resolve()
    workdir = _workspace_cwd(root=root_abs, cwd=cwd)
    run_env = _sanitized_env(clear_env=clear_env)
    path_value = _bwrap_path_value(run_env.get("PATH"))
    args: list[str] = ["bwrap", "--die-with-parent"]
    created_dirs: set[str] = set()
    if clear_env:
        args.append("--clearenv")
    if profile == "hardened":
        args.extend(["--unshare-pid", "--unshare-ipc", "--unshare-uts"])
        if unshare_cgroup:
            args.append("--unshare-cgroup")
    else:
        args.append("--unshare-pid")

    args.extend(["--bind", os.fspath(root_abs), "/workspace"])
    if profile == "hardened":
        for host_path, rel_posix_path in _protected_repo_paths(root_abs):
            args.extend(
                [
                    "--ro-bind",
                    os.fspath(host_path),
                    f"/workspace/{rel_posix_path}",
                ]
            )
    if profile == "hardened":
        for sys_dir in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(sys_dir).exists():
                args.extend(["--ro-bind", sys_dir, sys_dir])
        etc_dir = ensure_minimal_etc_dir(network=network)
        args.extend(
            [
                "--dir",
                "/etc",
                "--ro-bind",
                os.fspath(etc_dir / "passwd"),
                "/etc/passwd",
                "--ro-bind",
                os.fspath(etc_dir / "group"),
                "/etc/group",
                "--ro-bind",
                os.fspath(etc_dir / "nsswitch.conf"),
                "/etc/nsswitch.conf",
                "--ro-bind",
                os.fspath(etc_dir / "hosts"),
                "/etc/hosts",
                "--ro-bind",
                os.fspath(etc_dir / "resolv.conf"),
                "/etc/resolv.conf",
            ]
        )
        if Path("/etc/alternatives").exists():
            args.extend(["--ro-bind", "/etc/alternatives", "/etc/alternatives"])
        _add_hardened_java_config_bindings(args)
        _add_hardened_tls_bindings(args)
    else:
        for sys_dir in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
            if Path(sys_dir).exists():
                args.extend(["--ro-bind", sys_dir, sys_dir])

    args.extend(
        [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/home",
        ]
    )
    created_dirs.update({"/proc", "/dev", "/tmp", "/tmp/home"})
    _add_python_runtime_bindings(args, created_dirs=created_dirs)
    toolchain_env = _add_common_toolchain_bindings(
        args,
        path_value=path_value,
        created_dirs=created_dirs,
    )

    args.extend(
        [
            "--chdir",
            workdir,
            "--setenv",
            "HOME",
            "/tmp/home",
        ]
    )
    if path_value:
        args.extend(["--setenv", "PATH", path_value])
    lang_value = run_env.get("LANG")
    if lang_value:
        args.extend(["--setenv", "LANG", lang_value])
    for key, value in toolchain_env.items():
        args.extend(["--setenv", key, value])
    if network == "off":
        args.append("--unshare-net")
    args.extend(["sh", "-lc", cmd])
    return args, run_env


@dataclass(frozen=True)
class BwrapShellRunner:
    network: str = "off"
    clear_env: bool = True
    profile: str = "hardened"
    process_group_registry: ProcessGroupRegistry | None = None
    close_stdin: bool = False

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        args, run_env = _build_bwrap_argv(
            root=root,
            cwd=cwd,
            cmd=cmd,
            network=self.network,
            clear_env=self.clear_env,
            profile=self.profile,
            unshare_cgroup=_supports_bwrap_unshare_cgroup(),
        )
        return run_in_tracked_process_group(
            args,
            cwd=os.fspath(root.resolve()),
            env=run_env,
            timeout=timeout_s,
            registry=self.process_group_registry,
            origin="shell_run:bwrap",
            command_label=cmd,
            stdin=subprocess.DEVNULL if self.close_stdin else None,
        )


def _supports_bwrap_unshare_cgroup() -> bool:
    global _BWRAP_UNSHARE_CGROUP_SUPPORTED
    if _BWRAP_UNSHARE_CGROUP_SUPPORTED is not None:
        return _BWRAP_UNSHARE_CGROUP_SUPPORTED
    try:
        cp = subprocess.run(
            ["bwrap", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        _BWRAP_UNSHARE_CGROUP_SUPPORTED = False
        return False
    help_text = (cp.stdout or "") + (cp.stderr or "")
    _BWRAP_UNSHARE_CGROUP_SUPPORTED = cp.returncode == 0 and "--unshare-cgroup" in help_text
    return _BWRAP_UNSHARE_CGROUP_SUPPORTED


def _add_hardened_tls_bindings(args: list[str]) -> None:
    args.extend(["--dir", "/etc/ssl", "--dir", "/etc/ssl/certs"])
    if Path("/etc/ssl/certs").exists():
        args.extend(["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"])

    args.extend(["--dir", "/etc/pki", "--dir", "/etc/pki/tls", "--dir", "/etc/pki/tls/certs"])
    if Path("/etc/pki/tls/certs").exists():
        args.extend(["--ro-bind", "/etc/pki/tls/certs", "/etc/pki/tls/certs"])

    args.extend(["--dir", "/etc/ca-certificates"])
    if Path("/etc/ca-certificates").exists():
        args.extend(["--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates"])


def _build_docker_argv(
    *,
    root: Path,
    cwd: Path,
    cmd: str,
    container_name: str,
    network: str,
    docker_image: str,
    clear_env: bool,
    pids_limit: int | None,
    memory_limit: str | None,
    cpus: str | None,
    read_only_rootfs: bool,
    protect_repo_meta: bool,
    env_allowlist: tuple[str, ...],
    published_ports: tuple[tuple[str, int, int], ...] = (),
) -> tuple[list[str], dict[str, str]]:
    """Build docker run argv + parent-process env. Pure function."""

    root_abs = root.resolve()
    workdir = _workspace_cwd(root=root_abs, cwd=cwd)
    run_env = _sanitized_env(clear_env=clear_env)
    container_env = _container_env(clear_env, allowlist=env_allowlist)

    args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "-v",
        f"{os.fspath(root_abs)}:/workspace:rw",
        "-w",
        workdir,
    ]
    if protect_repo_meta or read_only_rootfs:
        for host_path, rel_posix_path in _protected_repo_paths(root_abs):
            args.extend(
                [
                    "-v",
                    f"{os.fspath(host_path)}:/workspace/{rel_posix_path}:ro",
                ]
            )
    if pids_limit is not None:
        args.extend(["--pids-limit", str(pids_limit)])
    if memory_limit is not None:
        args.extend(["--memory", memory_limit])
    if cpus is not None:
        args.extend(["--cpus", cpus])
    if read_only_rootfs:
        args.extend(["--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev"])
    for host, host_port, container_port in published_ports:
        try:
            publish_address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Docker published-port host must be an IP address") from exc
        if not publish_address.is_loopback:
            raise ValueError("Docker published ports must bind to a loopback address")
        if not (0 < host_port <= 65535 and 0 < container_port <= 65535):
            raise ValueError("Docker published ports must be between 1 and 65535")
        formatted_host = f"[{host}]" if ":" in host else host
        args.extend(["--publish", f"{formatted_host}:{host_port}:{container_port}/tcp"])
    if network == "off":
        args.extend(["--network", "none"])
    if os.name == "posix" and hasattr(os, "getuid") and hasattr(os, "getgid"):
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    args.extend(["--cap-drop=ALL", "--security-opt", "no-new-privileges"])
    for key, value in container_env.items():
        args.extend(["-e", f"{key}={value}"])
    args.extend([docker_image, "sh", "-lc", f"mkdir -p /tmp/home && {cmd}"])
    return args, run_env


def _docker_cleanup_container(
    container_name: str,
    *,
    cwd: str,
    env: dict[str, str],
    warning_callback: Callable[[str], None] | None,
    reason: str,
    quiet: bool = False,
) -> None:
    """Best-effort forced cleanup of a docker container by name."""

    if not quiet:
        _emit_warning(
            (f"Docker shell sandbox {container_name} cleanup after {reason}; killing container."),
            warning_callback=warning_callback,
        )
    for cleanup_args in (
        ["docker", "kill", "--signal=KILL", container_name],
        ["docker", "rm", "-f", container_name],
    ):
        try:
            subprocess.run(
                cleanup_args,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=cwd,
                env=env,
            )
        except Exception:  # noqa: BLE001
            # Docker cleanup is best-effort; callers must not fail while unwinding.
            _LOGGER.exception("Docker cleanup command failed for container %s", container_name)


@dataclass(frozen=True)
class DockerShellRunner:
    network: str = "off"
    docker_image: str = default_sandbox_docker_image("dev")
    clear_env: bool = True
    pids_limit: int | None = None
    memory_limit: str | None = None
    cpus: str | None = None
    read_only_rootfs: bool = False
    protect_repo_meta: bool = True
    env_allowlist: tuple[str, ...] = ()
    warning_callback: Callable[[str], None] | None = None
    close_stdin: bool = False

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        root_abs = root.resolve()
        container_name = f"alysis-sbx-{uuid.uuid4().hex[:12]}"
        args, run_env = _build_docker_argv(
            root=root_abs,
            cwd=cwd,
            cmd=cmd,
            container_name=container_name,
            network=self.network,
            docker_image=self.docker_image,
            clear_env=self.clear_env,
            pids_limit=self.pids_limit,
            memory_limit=self.memory_limit,
            cpus=self.cpus,
            read_only_rootfs=self.read_only_rootfs,
            protect_repo_meta=self.protect_repo_meta,
            env_allowlist=self.env_allowlist,
        )
        proc: subprocess.Popen[str] | None = None
        container_killed = False

        def cleanup_container(reason: str) -> None:
            nonlocal container_killed
            if container_killed:
                return
            container_killed = True
            _docker_cleanup_container(
                container_name,
                cwd=os.fspath(root_abs),
                env=run_env,
                warning_callback=self.warning_callback,
                reason=reason,
            )

        def kill_docker_cli() -> None:
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=os.fspath(root_abs),
                env=run_env,
                **({"stdin": subprocess.DEVNULL} if self.close_stdin else {}),
            )
            stdout, stderr = proc.communicate(timeout=timeout_s)
            return subprocess.CompletedProcess(
                args=args,
                returncode=proc.returncode if proc.returncode is not None else 1,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            cleanup_container("timeout")
            kill_docker_cli()
            raise
        except BaseException:
            if proc is not None:
                cleanup_container("exception")
                kill_docker_cli()
            raise
        finally:
            if proc is not None and proc.poll() is None:
                cleanup_container("unfinished process")
                kill_docker_cli()


def _emit_warning(message: str, *, warning_callback: Callable[[str], None] | None) -> None:
    if warning_callback is not None:
        warning_callback(message)
        return
    print(f"[alysis] {message}", file=sys.stderr)


def with_closed_stdin(runner: ShellRunner) -> ShellRunner:
    """Return an equivalent runner that never inherits the caller's stdin.

    Automated callers such as verification must not be able to block on an
    interactive prompt: nobody is watching the terminal, so a command that asks
    a question would otherwise wait until its timeout. Runners that do not
    execute anything (disabled/lazy) are returned unchanged.
    """

    if is_dataclass(runner) and any(field.name == "close_stdin" for field in fields(runner)):
        return replace(runner, close_stdin=True)  # type: ignore[type-var]
    return runner


def build_shell_runner_from_settings(
    settings: ShellSandboxSettings,
    root: Path,
    warning_callback: Callable[[str], None] | None = None,
    *,
    process_group_registry: ProcessGroupRegistry | None = None,
) -> ShellRunner:
    _ = root.resolve()

    # The Docker backend is deliberately not given the registry: `killpg` on the
    # `docker run` CLI does not stop the container, so teardown must go through
    # `_docker_cleanup_container` (which this runner already does on timeout and
    # on exception). This mirrors DockerBackgroundRunner's "direct" termination
    # mode.
    if settings.mode == "off":
        return HostShellRunner(process_group_registry=process_group_registry)

    is_linux = platform.system().lower() == "linux"
    has_bwrap = is_linux and shutil.which("bwrap") is not None
    has_docker = shutil.which("docker") is not None

    def fallback_or_error(*, reason: str) -> ShellRunner:
        if settings.mode == "warn":
            disabled_reason = f"Shell sandbox unavailable ({reason}); host fallback is disabled."
            _emit_warning(disabled_reason, warning_callback=warning_callback)
            return DisabledShellRunner(reason=disabled_reason)
        raise ConfigError(
            "Shell sandbox strict mode is enabled, but no usable backend is available: "
            f"{reason}. Install bubblewrap (Linux) or Docker, or set "
            "ALYSIS_SHELL_SANDBOX_MODE=off for explicit unsafe host execution. "
            "Run `alysis doctor sandbox` for a guided diagnosis or "
            "`alysis setup sandbox` after installing/starting Docker."
        )

    if settings.backend == "auto":
        if has_bwrap:
            return BwrapShellRunner(
                network=settings.network,
                clear_env=settings.clear_env,
                profile=settings.bwrap_profile,
                process_group_registry=process_group_registry,
            )
        if has_docker:
            return DockerShellRunner(
                network=settings.network,
                docker_image=settings.docker_image,
                clear_env=settings.clear_env,
                pids_limit=settings.docker_pids_limit,
                memory_limit=settings.docker_memory,
                cpus=settings.docker_cpus,
                read_only_rootfs=settings.docker_read_only,
                protect_repo_meta=settings.protect_repo_meta,
                env_allowlist=settings.docker_env_allowlist,
                warning_callback=warning_callback,
            )
        return fallback_or_error(reason="auto backend could not find bwrap or docker")

    if settings.backend == "bwrap":
        if has_bwrap:
            return BwrapShellRunner(
                network=settings.network,
                clear_env=settings.clear_env,
                profile=settings.bwrap_profile,
                process_group_registry=process_group_registry,
            )
        return fallback_or_error(reason="bwrap backend selected, but bubblewrap is not available")

    if settings.backend == "docker":
        if has_docker:
            return DockerShellRunner(
                network=settings.network,
                docker_image=settings.docker_image,
                clear_env=settings.clear_env,
                pids_limit=settings.docker_pids_limit,
                memory_limit=settings.docker_memory,
                cpus=settings.docker_cpus,
                read_only_rootfs=settings.docker_read_only,
                protect_repo_meta=settings.protect_repo_meta,
                env_allowlist=settings.docker_env_allowlist,
                warning_callback=warning_callback,
            )
        return fallback_or_error(reason="docker backend selected, but docker is not available")

    raise ConfigError(f"Unhandled shell sandbox backend: {settings.backend}")


def build_shell_runner(
    cfg: AppConfig,
    root: Path,
    warning_callback: Callable[[str], None] | None = None,
) -> ShellRunner:
    settings = resolve_shell_sandbox_settings(cfg)
    return build_shell_runner_from_settings(settings, root, warning_callback=warning_callback)
