from __future__ import annotations

from pathlib import Path

import typer
from platformdirs import user_data_dir

from ...config import ConfigError
from ._shared import _console

server_app = typer.Typer(add_completion=False, help="Server mode commands.")


@server_app.command("start")
def server_start(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
    port: int = typer.Option(7070, "--port", min=1, help="Port to bind."),
    data_dir: Path | None = typer.Option(
        None,
        "--data-dir",
        help="Server data directory (default: platform app data dir).",
    ),
) -> None:
    console = _console()
    try:
        import fastapi  # noqa: F401
        import uvicorn

        from ...server.app import create_app
        from ...server.settings import resolve_server_settings
    except ModuleNotFoundError as e:
        console.print(
            "[red]Server dependencies missing.[/red] "
            "Install server extras: [bold]pip install .[server][/bold]"
        )
        raise typer.Exit(code=2) from e

    default_data_dir = Path(user_data_dir("alysis-code-server", "alysis"))
    effective_data_dir = data_dir if data_dir is not None else default_data_dir

    try:
        settings = resolve_server_settings(
            host=host,
            port=port,
            data_dir=effective_data_dir,
        )
    except ConfigError as e:
        console.print(f"[red]Server config error:[/red] {e}")
        raise typer.Exit(code=2) from e

    def _app_factory():  # type: ignore[no-untyped-def]
        return create_app(settings)

    uvicorn.run(_app_factory, host=settings.host, port=settings.port, factory=True)
