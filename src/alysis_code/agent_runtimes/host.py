from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Literal, cast

from ..config import AppConfig
from ..surface.console import redact_console_text
from .base import RuntimeTurnRequest, RuntimeTurnResult
from .service import (
    RuntimeConnectionError,
    run_runtime_turn,
    runtime_connection_snapshot,
)


def validate_delegated_cli_options(
    *,
    base_url: str | None,
    temperature: float | None,
    max_steps: int | None,
    subagents: bool | None,
    verify_cmd: list[str] | None,
    api_key_env: str | None,
    api_key_stdin: bool,
    api_key: str | None,
    stream: bool | None = None,
    yes: bool = False,
    benchmark: bool = False,
    diagnostic_log: Path | None = None,
) -> None:
    """Fail visibly for native-only flags instead of silently ignoring them."""

    unsupported: list[str] = []
    if base_url is not None:
        unsupported.append("--base-url")
    if temperature is not None:
        unsupported.append("--temperature")
    if max_steps is not None:
        unsupported.append("--max-steps")
    if subagents is not None:
        unsupported.append("--subagents/--no-subagents")
    if verify_cmd:
        unsupported.append("--verify-cmd")
    if api_key_env is not None:
        unsupported.append("--api-key-env")
    if api_key_stdin:
        unsupported.append("--api-key-stdin")
    if api_key is not None:
        unsupported.append("--api-key")
    if stream is not None:
        unsupported.append("--stream/--no-stream")
    if yes:
        unsupported.append("--yes")
    if benchmark:
        unsupported.append("--benchmark")
    if diagnostic_log is not None:
        unsupported.append("--diagnostic-log")
    if unsupported:
        joined = ", ".join(unsupported)
        raise RuntimeConnectionError(
            f"Delegated execution does not support these native Alysis Code options: {joined}."
        )


def prepare_delegated_runtime(
    cfg: AppConfig,
    *,
    model: str | None = None,
    deadline_seconds: float | None = None,
) -> str:
    """Verify installation/auth and apply invocation-local settings."""

    snapshot = runtime_connection_snapshot(cfg)
    if not snapshot.probe.available:
        raise RuntimeConnectionError(
            snapshot.probe.detail or f"Runtime {snapshot.option.id!r} is not installed."
        )
    if not snapshot.account.verified or not snapshot.account.authenticated:
        raise RuntimeConnectionError(
            (snapshot.account.detail or "Runtime account is not connected.")
            + f" Run `alysis auth login {snapshot.option.id}`."
        )
    settings = cfg.agent_runtimes[snapshot.option.id]
    if model is not None:
        settings.model = str(model).strip() or None
    if deadline_seconds is not None:
        if deadline_seconds <= 0 or not math.isfinite(deadline_seconds):
            raise RuntimeConnectionError(
                "--deadline-seconds must be a finite number greater than zero."
            )
        settings.timeout_seconds = min(settings.timeout_seconds, float(deadline_seconds))
    return snapshot.option.id


def run_delegated_once(
    *,
    cfg: AppConfig,
    cwd: Path,
    instruction: str,
    mode: str,
    image_paths: tuple[Path, ...],
    no_log: bool,
    console: Any,
) -> int:
    normalized_mode = _normalized_mode(mode)
    if normalized_mode == "review":
        console.print(
            "[yellow]Review mode is enforced as read-only by this delegated adapter.[/yellow]"
        )
    request = RuntimeTurnRequest(
        prompt=instruction,
        cwd=cwd,
        mode=normalized_mode,
        images=image_paths,
        no_log=no_log,
    )
    result = run_runtime_turn(cfg, request)
    _render_turn_result(console, result)
    return 0 if result.ok else (result.exit_code or 1)


def run_delegated_chat(
    *,
    cfg: AppConfig,
    cwd: Path,
    mode: str,
    initial_images: tuple[Path, ...],
    no_log: bool,
    console: Any,
) -> None:
    if no_log:
        raise RuntimeConnectionError(
            "Delegated interactive chat cannot guarantee --no-log while resuming a provider "
            "session. Use `alysis run --no-log ...` for an ephemeral turn."
        )
    normalized_mode = _normalized_mode(mode)
    runtime_id = str(cfg.execution.runtime or "delegated runtime")
    console.print(
        f"[bold]Delegated chat:[/bold] {runtime_id} · mode {normalized_mode} · "
        "provider owns the agent loop and tools"
    )
    if normalized_mode == "review":
        console.print(
            "[yellow]Review mode is enforced as read-only by this delegated adapter.[/yellow]"
        )
    console.print(
        "[dim]Commands: /status · /new · /help · /exit. Native Alysis Code slash commands "
        "are unavailable in delegated chat.[/dim]"
    )
    session_id: str | None = None
    images = initial_images
    while True:
        try:
            prompt = str(console.input("[bold cyan]>[/bold cyan] ")).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("")
            return
        if not prompt:
            continue
        command = prompt.casefold()
        if command in {"/exit", "/quit", "exit", "quit"}:
            return
        if command == "/new":
            session_id = None
            console.print("[dim]Started a new delegated provider session.[/dim]")
            continue
        if command == "/status":
            status = runtime_connection_snapshot(cfg)
            console.print(
                f"Runtime: {status.option.label} · "
                f"authenticated {'yes' if status.account.authenticated else 'no'} · "
                f"session {session_id or 'new'}"
            )
            continue
        if command == "/help":
            console.print(
                "[dim]/status shows connection state; /new starts a new provider thread; "
                "/exit returns to the shell.[/dim]"
            )
            continue
        if prompt.startswith("/"):
            console.print(
                "[yellow]That native Alysis Code command is unavailable in delegated chat. "
                "Use /help.[/yellow]"
            )
            continue
        console.print("[dim]Delegated agent is working…[/dim]")
        result = run_runtime_turn(
            cfg,
            RuntimeTurnRequest(
                prompt=prompt,
                cwd=cwd,
                mode=normalized_mode,
                session_id=session_id,
                images=images,
            ),
        )
        images = ()
        _render_turn_result(console, result)
        if result.ok:
            session_id = result.session_id


def _normalized_mode(
    mode: str,
) -> Literal["readonly", "review", "auto"]:
    normalized = str(mode or "review").strip().lower()
    if normalized == "fullaccess":
        raise RuntimeConnectionError(
            "Delegated execution does not support fullaccess; use readonly, review, or auto."
        )
    if normalized not in {"readonly", "review", "auto"}:
        raise RuntimeConnectionError(f"Unsupported delegated execution mode: {mode!r}.")
    return cast(Literal["readonly", "review", "auto"], normalized)


_OSC_SEQUENCE_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_ANSI_SEQUENCE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_BIDI_CONTROL_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


def _safe_runtime_text(value: object) -> str:
    """Make provider-controlled output inert before it reaches a terminal."""

    clean = _OSC_SEQUENCE_RE.sub("", str(value or ""))
    clean = _ANSI_SEQUENCE_RE.sub("", clean)
    clean = _TERMINAL_CONTROL_RE.sub("", clean)
    clean = _BIDI_CONTROL_RE.sub("", clean)
    return redact_console_text(clean)


def _render_turn_result(console: Any, result: RuntimeTurnResult) -> None:
    if result.final_message:
        console.print(
            _safe_runtime_text(result.final_message),
            markup=False,
            highlight=False,
        )
    for warning in result.warnings:
        console.print(
            f"Runtime warning: {_safe_runtime_text(warning)}",
            style="yellow",
            markup=False,
            highlight=False,
        )
    if not result.ok:
        console.print(
            f"Delegated runtime error: {_safe_runtime_text(result.error or 'turn failed')}",
            style="red",
            markup=False,
            highlight=False,
        )


__all__ = [
    "prepare_delegated_runtime",
    "run_delegated_chat",
    "run_delegated_once",
    "validate_delegated_cli_options",
]
