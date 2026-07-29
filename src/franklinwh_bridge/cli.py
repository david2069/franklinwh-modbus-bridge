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


gateway_app = typer.Typer(help="Gateway management")
app.add_typer(gateway_app, name="gateway")


@gateway_app.command("diagnose")
def gateway_diagnose(gateway_id: str = typer.Argument(..., help="Gateway ID")):
    """Check TCP reachability for a gateway's Modbus (502) and Local API (9000) ports.

    This is a network-reachability check only. It deliberately does not
    attempt a live Modbus protocol read: if the bridge is already running
    (the normal case), a standalone CLI process has no safe way to reuse
    its live Modbus session, and opening a second one is a known corruption
    trigger on the aGate (see docs/vendor-issues.md). For the full
    diagnostic, including a protocol-level check against the running
    bridge's own session, use the Web UI's Diagnose button or
    'POST /api/gateways/{id}/diagnose'.
    """
    async def _diagnose():
        from franklinwh_bridge.config.manager import AppConfig
        from franklinwh_bridge.gateway.net_probe import tcp_probe
        from franklinwh_bridge.store.db import get_gateway, init_db

        config = AppConfig()
        db = await init_db(config.db_path)
        row = await get_gateway(db, gateway_id)
        await db.close()

        if row is None:
            typer.echo(f"Gateway '{gateway_id}' not found")
            raise typer.Exit(1)

        host = row["host"]
        modbus_result = await tcp_probe(host, row["port"])
        local_api_result = await tcp_probe(host, 9000)

        typer.echo(f"Gateway '{gateway_id}' at {host}")
        for label, result in (
            (f"Modbus TCP ({row['port']})", modbus_result),
            ("Local API (9000)", local_api_result),
        ):
            if result.ok:
                typer.echo(f"  {label}: reachable ({result.latency_ms} ms)")
            else:
                typer.echo(f"  {label}: unreachable ({result.error})")

        typer.echo(
            "\nFor a full diagnosis (including a live Modbus protocol check "
            "against the running bridge), use the Web UI's Diagnose button "
            f"or: curl -X POST http://localhost:8099/api/gateways/{gateway_id}/diagnose"
        )

    asyncio.run(_diagnose())


DEFAULT_BRIDGE_URL = "http://localhost:8099"


async def _call_bridge_api(url: str, path: str) -> dict:
    """POST to the running bridge's own REST API.

    Restart and force-healthcheck act on live state inside the bridge's
    server process (the registry, the poller's Modbus session) — there is
    no in-process equivalent to call from a separate one-shot CLI process,
    unlike 'diagnose' (network-only) or 'backup'/'config' (DB/filesystem
    only). This talks to the already-running bridge over HTTP instead.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{url.rstrip('/')}{path}")
        if resp.status_code >= 400:
            detail = resp.json().get("detail", resp.text) if resp.content else resp.text
            typer.echo(f"Error ({resp.status_code}): {detail}")
            raise typer.Exit(1)
        return resp.json()
    except httpx.ConnectError:
        typer.echo(f"Could not reach the bridge at {url} — is it running?")
        raise typer.Exit(1) from None


@gateway_app.command("restart")
def gateway_restart(
    gateway_id: str = typer.Argument(..., help="Gateway ID"),
    url: str = typer.Option(DEFAULT_BRIDGE_URL, help="Running bridge's base URL"),
):
    """Reconnect a gateway's Modbus session (tear down and recreate).

    Fixes a wedged/dead session without restarting the whole bridge
    process. Requires the bridge to already be running.
    """
    async def _restart():
        data = await _call_bridge_api(url, f"/api/gateways/{gateway_id}/restart")
        typer.echo(f"Gateway '{gateway_id}' restarted: {data}")

    asyncio.run(_restart())


@gateway_app.command("healthcheck")
def gateway_healthcheck(
    gateway_id: str = typer.Argument(..., help="Gateway ID"),
    url: str = typer.Option(DEFAULT_BRIDGE_URL, help="Running bridge's base URL"),
):
    """Force an immediate TCP health probe instead of waiting for the
    periodic health checker (default: every 60s). Requires the bridge to
    already be running.
    """
    async def _healthcheck():
        data = await _call_bridge_api(url, f"/api/gateways/{gateway_id}/healthcheck")
        typer.echo(f"Gateway '{gateway_id}' health: {data.get('health')}")

    asyncio.run(_healthcheck())


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
