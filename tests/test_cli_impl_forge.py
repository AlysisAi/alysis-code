from __future__ import annotations

from typer.testing import CliRunner

from alysis_code.cli_impl import forge as forge_impl


def test_path_binding_source_works_without_runtime_injection() -> None:
    assert forge_impl._path_binding_source() == "cwd"


def test_path_binding_source_detects_explicit_path() -> None:
    app = forge_impl.typer.Typer()
    observed: dict[str, str] = {}

    @app.command()
    def command(
        ctx: forge_impl.typer.Context,
        path: str = forge_impl.typer.Option(".", "--path"),
    ) -> None:
        _ = path
        observed["source"] = forge_impl._path_binding_source(ctx)

    result = CliRunner().invoke(app, ["--path", "/tmp/example"])

    assert result.exit_code == 0
    assert observed["source"] == "explicit_path"
