"""Legal-notice acknowledgement.

The notice is shown at startup and in the log, but neither proves anyone read
it: a service someone else installed can run for months without its operator
ever seeing a console. A modal that blocks until acknowledged is the only place
the user is actually in front of the words.

Acknowledgement is per **user** and per **notice version**:

* per user, because this bridge has accounts and roles — one person accepting
  does not make it true of the next person to log in;
* per version, because consent to wording somebody never saw is not consent.

The state lives in the database rather than in ``localStorage`` so it survives
a browser change and, more importantly, so it exists as a record. A legal
acknowledgement nobody can look up afterwards is not much of one.
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from franklinwh_bridge import disclaimer
from franklinwh_bridge.api.auth import require_auth, require_role
from franklinwh_bridge.store.db import (
    get_disclaimer_acks,
    has_acked_disclaimer,
    log_startup_event,
    record_disclaimer_ack,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/disclaimer", tags=["disclaimer"])

_REQUIRE_ADMIN = Depends(require_role("admin"))


class AckRequest(BaseModel):
    """``agreed`` false is a real answer, not a no-op.

    The modal's Continue button is enabled either way: refusing to let someone
    close a dialog is not consent, it is a hostage situation. Declining simply
    isn't recorded, so the notice comes back next time — which is the behaviour
    asked for, and is honest about what did and didn't happen.
    """

    agreed: bool = True


def _client_ip(request: Request) -> str:
    # Behind the HA ingress proxy the socket peer is the proxy, so prefer the
    # forwarded address when one is present.
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("")
async def get_disclaimer(request: Request, user: dict = Depends(require_auth)):
    """The notice text, and whether this user has already accepted this version.

    Returns the text as well as the flag so the UI never keeps its own copy of
    the wording — one source, and a version bump can't leave a stale paragraph
    rendered next to a fresh version number.
    """
    db: aiosqlite.Connection = request.app.state.db
    acked = await has_acked_disclaimer(db, user["id"], disclaimer.VERSION)
    return {
        "version": disclaimer.VERSION,
        "acknowledged": acked,
        "title": disclaimer.MODAL_TITLE,
        "paragraphs": list(disclaimer.MODAL_PARAGRAPHS),
        "agree_label": disclaimer.MODAL_AGREE,
        "docs_url": disclaimer.DOCS_URL,
        "issues_url": disclaimer.ISSUES_URL,
    }


@router.post("")
async def ack_disclaimer(
    request: Request, body: AckRequest, user: dict = Depends(require_auth)
):
    """Record acceptance. Declining is answered honestly rather than stored."""
    db: aiosqlite.Connection = request.app.state.db

    if not body.agreed:
        logger.info(
            "Legal notice v%s shown to %s and NOT accepted — will prompt again",
            disclaimer.VERSION,
            user.get("username", user["id"]),
        )
        return {"version": disclaimer.VERSION, "acknowledged": False}

    ip = _client_ip(request)
    row = await record_disclaimer_ack(
        db,
        user["id"],
        disclaimer.VERSION,
        client_ip=ip,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # Both surfaces: the app log the user can read in the Logs tab, and the
    # persisted startup_log so it survives a log-buffer roll. An acknowledgement
    # that is only in a ring buffer is one that quietly disappears.
    logger.warning(
        "Legal notice v%s ACCEPTED by %s from %s",
        disclaimer.VERSION,
        user.get("username", user["id"]),
        ip or "unknown",
    )
    await log_startup_event(
        db,
        "disclaimer_ack",
        f"v{disclaimer.VERSION} accepted by {user.get('username', user['id'])} "
        f"from {ip or 'unknown'}",
    )

    return {"version": disclaimer.VERSION, "acknowledged": True, "ack": row}


@router.get("/acks")
async def list_acks(request: Request, _user: dict = _REQUIRE_ADMIN, limit: int = 200):
    """The acknowledgement trail. Admin-only — it is a record of who agreed to
    what, which is nobody else's business."""
    db: aiosqlite.Connection = request.app.state.db
    return {"acks": await get_disclaimer_acks(db, max(1, min(limit, 1000)))}
