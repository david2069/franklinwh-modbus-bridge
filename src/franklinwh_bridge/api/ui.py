"""Web UI routes — serves the SPA shell and command endpoint."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from franklinwh_bridge import disclaimer
from franklinwh_bridge.api.auth import get_current_user, require_capability

# Hoisted so it is built once at import rather than per-request: ruff's B008
# allows Depends() itself in a default, but not a factory call nested in it.
_REQUIRE_CONTROL = require_capability("control")

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "static"
logger = logging.getLogger(__name__)

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
    # See GatewayCommandRequest: numeric commands (watts/seconds/percent)
    # must not 422 just because the client sent a JSON number.
    value: str

    @field_validator("value", mode="before")
    @classmethod
    def _stringify_scalars(cls, v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return v


def _base_path(request: Request) -> str:
    ingress_path = request.headers.get("X-Ingress-Path", "")
    return ingress_path.rstrip("/") if ingress_path else ""


def _home_for(base_path: str, user: dict) -> str:
    """Role-based landing page. Admins get the full SPA; user/viewer roles get
    the simplified mobile-first end-user dashboard at /user."""
    if user.get("role") in ("user", "viewer"):
        return f"{base_path}/user"
    return f"{base_path}/"


#: Whether this process has already logged the notice for a browser client.
#: Per-process, deliberately: the point is that somebody opening the UI sees it
#: recorded once per run, not that it is repeated on every page load until the
#: log is useless.
_disclaimer_shown = False


def _log_disclaimer_once(request: Request) -> None:
    """Record the legal notice the first time a browser reaches the UI.

    Startup already logs it, but a bridge that has been up for weeks has that
    line long since scrolled away — and the person who needs to read it is
    whoever just opened the page, who may not be whoever started the service.
    """
    global _disclaimer_shown
    if _disclaimer_shown:
        return
    _disclaimer_shown = True
    client = request.client.host if request.client else "unknown"
    logger.warning("First UI connection from %s. %s", client, disclaimer.SHORT)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict | None = Depends(get_current_user)):
    """Serve the main SPA shell. Unauthenticated → /login; non-admin → /user."""
    _log_disclaimer_once(request)
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
async def send_command(
    body: CommandRequest,
    request: Request,
    _user: dict = Depends(_REQUIRE_CONTROL),
):
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
