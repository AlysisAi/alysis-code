from __future__ import annotations

import json

import typer

from ...ide.health import health_payload
from ...ide.stdio_bridge import run_stdio_bridge

ide_bridge_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="Run the Alysis Code IDE protocol bridge.",
)


@ide_bridge_app.callback(invoke_without_command=True)
def ide_bridge(
    ctx: typer.Context,
    stdio: bool = typer.Option(False, "--stdio", help="Run protocol v1 over stdio JSONL."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if stdio:
        raise typer.Exit(code=run_stdio_bridge())
    typer.echo(ctx.get_help())


@ide_bridge_app.command("health")
def ide_bridge_health() -> None:
    typer.echo(json.dumps(health_payload(), ensure_ascii=True, sort_keys=True))
