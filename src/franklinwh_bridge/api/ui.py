"""Web UI routes — serves the SPA shell and command endpoint."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Cache-bust token — changes on each server restart
_CACHE_BUST = str(int(time.time()))

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"])


class CommandRequest(BaseModel):
    slug: str
    value: str


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main SPA shell."""
    # Support HA ingress path
    ingress_path = request.headers.get("X-Ingress-Path", "")
    base_path = ingress_path.rstrip("/") if ingress_path else ""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"base_path": base_path, "cache_bust": _CACHE_BUST},
    )


@router.post("/api/command")
async def send_command(body: CommandRequest, request: Request):
    """Dispatch a control command (same path as MQTT commands).

    Defaults to the "default" gateway. Use POST /api/gateways/{gw_id}/command
    for gateway-specific commands (added in MG-3).
    """
    # Resolve command handler from registry (or legacy fallback)
    registry = getattr(request.app.state, "registry", None)
    command_handler = None
    if registry:
        inst = registry.get("default")
        if inst:
            command_handler = inst.command_handler
    if command_handler is None:
        command_handler = getattr(request.app.state, "command_handler", None)
    if command_handler is None:
        return {"ok": False, "slug": body.slug, "result": "Command handler not available"}

    await command_handler.handle_command(body.slug, body.value)
    return {
        "ok": True,
        "slug": body.slug,
        "result": command_handler.state.last_result or "Sent",
    }
