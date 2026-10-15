"""Support info for a GitHub issue — a redacted summary of this install.

Admin-only. Reads are otherwise open to any signed-in role, but this one
describes the whole configuration (gateways, modules, broker source, users by
role), so it carries an explicit admin gate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from franklinwh_bridge.api.auth import require_role
from franklinwh_bridge.support.bundle import build_support_info, render_markdown

router = APIRouter(
    prefix="/api/support",
    tags=["support"],
    dependencies=[Depends(require_role("admin"))],
)


class SupportInfo(BaseModel):
    """The bundle (``info``) and the same as ready-to-paste Markdown."""

    info: dict[str, Any]
    markdown: str


@router.get("/info", response_model=SupportInfo)
async def get_support_info(request: Request) -> SupportInfo:
    """Redacted summary of this install, as JSON and as Markdown.

    No serials, IPs, host names, ports, MAC addresses, location, usernames,
    emails, HA URLs or tokens, passwords, MQTT credentials or client id, or
    device names. Log messages are scrubbed by pattern — review before posting.
    """
    info = await build_support_info(request.app.state)
    return SupportInfo(info=info, markdown=render_markdown(info))
