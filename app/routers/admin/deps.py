"""Shared lookups used by several admin routers.

Extracted after the routers/admin.py -> routers/admin/ split made the
duplication visible: the same "load DiscordConfig or 400" and "load
Occurrence+EventDefinition or 404" blocks were copy-pasted across
occurrences.py, discord_sync.py, and discord_config.py. Fixing the message
or status code for either now happens once instead of eight times.

get_current_tenant is the multi-tenant addition: every admin route now
operates within one Tenant's context, selected via the X-Tenant-Slug
header (same shared-secret-header pattern as X-Admin-Key in services/auth.py)
rather than a path segment, so route paths didn't need to change. This is
a bridge-period mechanism — once real sessions exist (Phase 4 of the
multi-tenant design), tenant selection can come from the session instead,
without another route-path change.
"""
import os
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, Occurrence, PostLog, Tenant

# Falls back to this when a Tenant's own bot_token is null — see
# models.db.Tenant's docstring for why sharing one token is the default.
PLATFORM_BOT_TOKEN = os.environ.get("PLATFORM_BOT_TOKEN", "")


async def get_current_tenant(
    x_tenant_slug: str = Header(default=""), db: AsyncSession = Depends(get_db)
) -> Tenant:
    if not x_tenant_slug:
        raise HTTPException(status_code=400, detail="X-Tenant-Slug header is required")
    result = await db.execute(select(Tenant).where(Tenant.slug == x_tenant_slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail=f"No such tenant: {x_tenant_slug}")
    return tenant


@dataclass
class DiscordCreds:
    bot_token: str
    guild_id: str


async def get_discord_config(tenant: Tenant = Depends(get_current_tenant)) -> DiscordCreds:
    token = tenant.bot_token or PLATFORM_BOT_TOKEN
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No Discord bot token set for this tenant, and no platform fallback configured",
        )
    return DiscordCreds(bot_token=token, guild_id=tenant.guild_id)


async def get_occurrence_with_event(
    occ_id: int, tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
) -> tuple[Occurrence, EventDefinition]:
    result = await db.execute(
        select(Occurrence, EventDefinition)
        .join(EventDefinition)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .where(
            Occurrence.id == occ_id,
            or_(
                Occurrence.tenant_id == tenant.id,
                (EventDefinition.scope == "kingdom-wide") & (Tenant.kingdom_id == tenant.kingdom_id),
            ),
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    return row


async def find_post_log(db: AsyncSession, tenant_id: int, event_name: str, occurrence_date) -> PostLog | None:
    """Not a FastAPI dependency — callers need different behavior when no
    row is found (some treat it as fine, some as a 404), so this stays a
    plain helper rather than one more Depends with baked-in error handling."""
    result = await db.execute(
        select(PostLog).where(
            PostLog.tenant_id == tenant_id,
            PostLog.event_name == event_name,
            PostLog.occurrence_date == occurrence_date,
        )
    )
    return result.scalar_one_or_none()
