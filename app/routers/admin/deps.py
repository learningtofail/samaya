"""Shared lookups and permission checks used across the admin routers.

Phase 4 replaced the bridge-period X-Admin-Key + X-Tenant-Slug model with
real sessions: get_current_user reads the signed session cookie
(services/sessions.py); get_current_tenant still reads X-Tenant-Slug (kept
as the tenant-selection mechanism, so route paths didn't need to change
again) but now also checks the logged-in user actually has UserTenant
access to it — the bridge period never enforced that, since the shared
key implicitly trusted every holder with every tenant.
"""
import os
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, Occurrence, PostLog, Tenant, User, UserTenant
from services.sessions import SESSION_COOKIE_NAME, read_session_token

# Falls back to this when a Tenant's own bot_token is null — see
# models.db.Tenant's docstring for why sharing one token is the default.
PLATFORM_BOT_TOKEN = os.environ.get("PLATFORM_BOT_TOKEN", "")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    user_id = read_session_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


async def require_superadmin(user: User = Depends(get_current_user)) -> User:
    if not user.is_superadmin:
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return user


async def get_current_tenant(
    x_tenant_slug: str = Header(default=""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    if not x_tenant_slug:
        raise HTTPException(status_code=400, detail="X-Tenant-Slug header is required")
    result = await db.execute(select(Tenant).where(Tenant.slug == x_tenant_slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail=f"No such tenant: {x_tenant_slug}")

    if user.is_superadmin:
        return tenant

    access = await db.execute(
        select(UserTenant).where(UserTenant.user_id == user.id, UserTenant.tenant_id == tenant.id)
    )
    if not access.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="You don't have access to this tenant")
    return tenant


async def require_tenant_owner(
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """For actions scoped to one tenant that only its owner (or a
    superadmin) should take — inviting/removing coordinators, changing
    the tenant's own settings. A plain coordinator passes
    get_current_tenant's access check but not this one."""
    if user.is_superadmin:
        return tenant
    result = await db.execute(
        select(UserTenant).where(
            UserTenant.user_id == user.id, UserTenant.tenant_id == tenant.id, UserTenant.role == "owner"
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Owner access required for this tenant")
    return tenant


async def check_kingdom_coordinator(db: AsyncSession, user: User, kingdom_id: int):
    """Not a FastAPI dependency — only some events (scope='kingdom-wide')
    need this check, not every route that touches EventDefinition, so
    it's called conditionally from inside create_event/update_event
    rather than declared as a blanket Depends()."""
    if user.is_superadmin:
        return
    from models.db import UserKingdom
    result = await db.execute(
        select(UserKingdom).where(UserKingdom.user_id == user.id, UserKingdom.kingdom_id == kingdom_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=403,
            detail="Creating or editing a kingdom-wide event requires kingdom coordinator access",
        )


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
    from sqlalchemy import or_
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
