"""Web UI routes — serves the SPA shell and command endpoint."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from franklinwh_bridge.api.auth import get_current_user, require_auth

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"])


def _cache_bust() -> str:
    """Static-asset cache-bust token = newest static-file mtime. Because src is
    hot-mounted, editing a .js/.css bumps this on the very next page load — no
    server restart needed — so browsers never serve stale JS against fresh HTML.
    Falls back to the process start time if the dir can't be walked."""
    try:
        latest = max(
            f.stat().st_mtime for f in _STATIC_DIR.rglob("*.js") if f.is_file()
        )
        return str(int(latest))
    except (ValueError, OSError):
        return str(int(time.time()))


class CommandRequest(BaseModel):
    slug: str
    value: str


def _base_path(request: Request) -> str:
    ingress_path = request.headers.get("X-Ingress-Path", "")
    return ingress_path.rstrip("/") if ingress_path else ""


def _home_for(base_path: str, user: dict) -> str:
    """Role-based landing page. Admins get the full SPA; user/viewer roles get
    the simplified mobile-first end-user dashboard at /user."""
    if user.get("role") in ("user", "viewer"):
        return f"{base_path}/user"
    return f"{base_path}/"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict | None = Depends(get_current_user)):
    """Serve the main SPA shell. Unauthenticated → /login; non-admin → /user."""
    base_path = _base_path(request)
    if user is None:
        return RedirectResponse(f"{base_path}/login", status_code=302)
    if user.get("role") in ("user", "viewer"):
        return RedirectResponse(f"{base_path}/user", status_code=302)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"base_path": base_path, "cache_bust": _cache_bust()},
    )


@router.get("/user", response_class=HTMLResponse)
async def user_dashboard(request: Request, user: dict | None = Depends(get_current_user)):
    """The simplified, mobile-first end-user dashboard. Any signed-in role may
    view it (admins can preview it here); unauthenticated → /login."""
    base_path = _base_path(request)
    if user is None:
        return RedirectResponse(f"{base_path}/login", status_code=302)
    return templates.TemplateResponse(
        request,
        "user.html",
        {"base_path": base_path, "cache_bust": _cache_bust()},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: dict | None = Depends(get_current_user)):
    """The login form. Already-authenticated users go to their role's home."""
    base_path = _base_path(request)
    if user is not None:
        return RedirectResponse(_home_for(base_path, user), status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"base_path": base_path, "cache_bust": _cache_bust()}
    )


@router.post("/api/command")
async def send_command(body: CommandRequest, request: Request, _user: dict = Depends(require_auth)):
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
        "ok": command_handler.state.last_success,
        "slug": body.slug,
        "result": command_handler.state.last_result or "Sent",
    }
