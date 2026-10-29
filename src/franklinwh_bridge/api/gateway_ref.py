"""A gateway in an API path is named by its ID or its serial (#296 item 42).

``/api/gateways/{ref}/…`` takes either. Two parts, so that nobody signed out
learns which serials exist:

- :class:`GatewayRefMiddleware` runs before routing and only rewrites a known
  serial to its gateway's ID. It never answers a request itself.
- :func:`check_gateway_ref` runs after the auth dependency and rejects a
  malformed reference (422) or one no gateway has (404), with the reason in
  ``detail``, and logs each rejection.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote, unquote

from fastapi import Depends, HTTPException, Request

from franklinwh_bridge.api.auth import _READ_METHODS, require_auth
from franklinwh_bridge.gateway.modules import ROLE_CAPABILITIES
from franklinwh_bridge.store.db import get_gateways

logger = logging.getLogger(__name__)

_PATH = re.compile(r"^/api/gateways/([^/]+)(/.*)?$")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")
MAX_LEN = 63  # the longest gateway ID the API accepts


def malformed(ref: str) -> str | None:
    """Why ``ref`` can't be a gateway ID or serial, or None."""
    if not ref.strip():
        return "it is empty"
    if len(ref) > MAX_LEN:
        return f"it is {len(ref)} characters long; an ID or serial has at most {MAX_LEN}"
    if _BAD_CHARS.search(ref):
        return "it contains a control character or '\\'"
    return None


async def resolve(db: Any, ref: str) -> str | None:
    """The ID of the gateway whose ID or serial is ``ref``, or None."""
    rows = await get_gateways(db)
    if any(r["id"] == ref for r in rows):
        return ref
    for r in rows:
        if r.get("serial") and r["serial"] == ref:
            return r["id"]
    return None


class GatewayRefMiddleware:
    """Rewrite ``/api/gateways/<serial>/…`` to the gateway's ID before routing."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            m = _PATH.match(scope["path"])
            db = getattr(getattr(scope.get("app"), "state", None), "db", None)
            if m and db is not None:
                ref = unquote(m.group(1))
                if not malformed(ref):
                    gw_id = await resolve(db, ref)
                    if gw_id is not None and gw_id != ref:
                        path = f"/api/gateways/{gw_id}{m.group(2) or ''}"
                        scope = dict(scope, path=path,
                                     raw_path=f"/api/gateways/{quote(gw_id, safe='')}"
                                              f"{m.group(2) or ''}".encode())
        await self.app(scope, receive, send)


async def check_gateway_ref(request: Request, user: dict = Depends(require_auth)) -> None:
    """Dependency: 422 for a malformed gateway reference, 404 for an unknown one."""
    ref = request.path_params.get("gw_id")
    if ref is None:
        return
    if (request.method not in _READ_METHODS and user.get("role") != "admin"
            and "control" not in ROLE_CAPABILITIES.get(user.get("role", ""), set())):
        return  # the route's own gate refuses this write with 403 first
    why = malformed(ref)
    if why:
        _reject(request, user, 422, f"Malformed gateway reference: {why}")
    if await resolve(request.app.state.db, ref) is None:
        _reject(request, user, 404, f"No gateway has the ID or serial '{ref}'")


def _reject(request: Request, user: dict, status: int, reason: str) -> None:
    logger.warning("API %s %s rejected (%d): %s [by %s]", request.method, request.url.path,
                   status, reason, user.get("username") or "?")
    raise HTTPException(status, reason)
