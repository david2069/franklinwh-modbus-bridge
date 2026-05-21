"""Typer CLI entry point."""

import typer

app = typer.Typer(name="bridge", help="FranklinWH Modbus Bridge CLI")


@app.command()
def run():
    """Start the bridge."""
    import uvicorn

    uvicorn.run("franklinwh_bridge.main:app", host="0.0.0.0", port=8099)


@app.command()
def version():
    """Show version."""
    from franklinwh_bridge import __version__

    typer.echo(f"franklinwh-modbus-bridge {__version__}")


if __name__ == "__main__":
    app()
