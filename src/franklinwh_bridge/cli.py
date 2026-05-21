"""Typer CLI entry point — thin caller into the API layer (in-process)."""

from __future__ import annotations

import asyncio
import json

import typer

app = typer.Typer(name="bridge", help="FranklinWH Modbus Bridge CLI")


@app.command()
def run(
    host: str = typer.Option("0.0.0.0", help="Bind address"),
    port: int = typer.Option(8099, help="Port"),
):
    """Start the bridge server."""
    import uvicorn

    uvicorn.run("franklinwh_bridge.main:app", host=host, port=port)


@app.command()
def version():
    """Show version."""
    from franklinwh_bridge import __version__

    typer.echo(f"franklinwh-modbus-bridge {__version__}")


@app.command()
def status():
    """Show component status."""
    async def _status():
        from franklinwh_bridge.config.manager import AppConfig
        from franklinwh_bridge.store.db import get_schema_version, init_db

        config = AppConfig()
        if not config.db_path.exists():
            typer.echo("Database not found. Run 'bridge run' first.")
            raise typer.Exit(1)

        db = await init_db(config.db_path)
        ver = await get_schema_version(db)
        await db.close()

        typer.echo(f"Environment: {config.environment}")
        typer.echo(f"Data dir:    {config.data_dir}")
        typer.echo(f"DB path:     {config.db_path}")
        typer.echo(f"Schema:      v{ver}")

    asyncio.run(_status())


config_app = typer.Typer(help="Configuration management")
app.add_typer(config_app, name="config")


@config_app.command("get")
def config_get(key: str = typer.Argument("all", help="Config key or 'all'")):
    """Get a configuration value."""
    async def _get():
        from franklinwh_bridge.config.manager import AppConfig
        from franklinwh_bridge.store.db import init_db

        config = AppConfig()
        db = await init_db(config.db_path)

        if key == "all":
            rows = {}
            async with db.execute("SELECT key, value FROM app_config") as cursor:
                async for row in cursor:
                    rows[row[0]] = row[1]
            typer.echo(json.dumps(rows, indent=2))
        else:
            async with db.execute(
                "SELECT value FROM app_config WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                typer.echo(f"{key}={row[0]}")
            else:
                typer.echo(f"Key '{key}' not found")
                raise typer.Exit(1)
        await db.close()

    asyncio.run(_get())


@config_app.command("set")
def config_set(key: str, value: str):
    """Set a configuration value."""
    async def _set():
        from franklinwh_bridge.config.manager import AppConfig
        from franklinwh_bridge.store.db import init_db

        config = AppConfig()
        db = await init_db(config.db_path)
        await db.execute(
            "INSERT INTO app_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()
        await db.close()
        typer.echo(f"{key}={value}")

    asyncio.run(_set())


models_app = typer.Typer(help="SunSpec model catalog")
app.add_typer(models_app, name="models")


@models_app.command("refresh")
def models_refresh(
    host: str = typer.Option(None, help="Override gateway host"),
    port: int = typer.Option(502, help="Modbus port"),
):
    """Re-capture the SunSpec model catalog from the aGate."""
    typer.echo("Model refresh requires a running bridge or direct aGate connection.")
    typer.echo("Use the REST API: POST /api/models/refresh")


backup_app = typer.Typer(help="Backup and restore")
app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def backup_create(label: str = typer.Option(None, help="Optional label")):
    """Create a manual backup."""
    async def _create():
        from franklinwh_bridge.config.manager import AppConfig
        from franklinwh_bridge.store.backup import BackupManager

        config = AppConfig()
        mgr = BackupManager(config.db_path, config.backup_dir)
        info = await mgr.create(label=label)
        typer.echo(f"Created: {info.name} ({info.size_bytes} bytes)")

    asyncio.run(_create())


@backup_app.command("list")
def backup_list():
    """List available backups."""
    from franklinwh_bridge.config.manager import AppConfig
    from franklinwh_bridge.store.backup import BackupManager

    config = AppConfig()
    mgr = BackupManager(config.db_path, config.backup_dir)
    backups = mgr.list_backups()

    if not backups:
        typer.echo("No backups found.")
        return

    for b in backups:
        from datetime import UTC, datetime

        ts = datetime.fromtimestamp(b.created_at, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        typer.echo(f"  {b.name}  schema=v{b.schema_version}  {ts}  {b.size_bytes}B")


@backup_app.command("restore")
def backup_restore(name: str = typer.Argument(..., help="Backup name")):
    """Restore from a backup."""
    async def _restore():
        from franklinwh_bridge.config.manager import AppConfig
        from franklinwh_bridge.store.backup import BackupManager

        config = AppConfig()
        mgr = BackupManager(config.db_path, config.backup_dir)

        backup_path = config.backup_dir / f"{name}.zip"
        if not backup_path.exists():
            typer.echo(f"Backup not found: {name}")
            raise typer.Exit(1)

        await mgr.restore(backup_path)
        typer.echo(f"Restored from {name}")

    asyncio.run(_restore())


if __name__ == "__main__":
    app()
