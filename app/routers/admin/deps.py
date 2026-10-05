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

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import DiscordServer, Tenant, User, UserTenant
from services.sessions import SESSION_COOKIE_NAME, read_session_token

# Fallback bot token for a Discord server that has none of its own.
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


async def get_current_tenants(
    x_tenant_slug: str = Header(default=""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Tenant]:
    """Combined-mode variant of get_current_tenant, for read/list endpoints
    only (see spec §14.3): 'X-Tenant-Slug: *' means every tenant the caller
    holds UserTenant access to (or every tenant in the deployment, for a
    superadmin), not one. Write endpoints keep depending on
    get_current_tenant directly, which has no notion of '*' and still
    requires exactly one real, access-checked tenant — combined mode is a
    viewing convenience, not a way to act on several tenants in one call.
    """
    if x_tenant_slug != "*":
        tenant = await get_current_tenant(x_tenant_slug=x_tenant_slug, user=user, db=db)
        return [tenant]

    if user.is_superadmin:
        result = await db.execute(select(Tenant))
        return list(result.scalars().all())

    result = await db.execute(
        select(Tenant).join(UserTenant, UserTenant.tenant_id == Tenant.id)
        .where(UserTenant.user_id == user.id)
    )
    tenants = list(result.scalars().all())
    if not tenants:
        raise HTTPException(status_code=403, detail="You don't have access to any tenant")
    return tenants


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


async def require_not_viewer(
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """For any mutating (create/update/delete) action on a tenant's data —
    spec §31's read-only Viewer role passes get_current_tenant's plain
    access check (a viewer can still load every GET endpoint) but is
    blocked here. Unlike require_tenant_owner this doesn't require
    'owner' specifically: both 'owner' and 'coordinator' may write, only
    'viewer' may not, so the check is a rejection rather than a
    match-required lookup."""
    if user.is_superadmin:
        return tenant
    result = await db.execute(
        select(UserTenant).where(UserTenant.user_id == user.id, UserTenant.tenant_id == tenant.id)
    )
    access = result.scalar_one_or_none()
    if access and access.role == "viewer":
        raise HTTPException(status_code=403, detail="Viewer access is read-only")
    return tenant


async def check_kingdom_coordinator(
    db: AsyncSession, user: User, kingdom_id: int,
    detail: str = "Creating or editing a kingdom-wide event requires kingdom coordinator access",
):
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
            detail=detail,
        )


@dataclass
class DiscordCreds:
    bot_token: str
    guild_id: str


async def get_discord_config(
    server_id: int | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> DiscordCreds:
    """The alliance's primary server, or, with `server_id`, any server of its
    Kingdom (spec §68): an Audience destination's channel and role pickers
    load from the server the destination uses."""
    server = tenant.server
    if server_id is not None and server_id != tenant.server_id:
        server = await db.get(DiscordServer, server_id)
        if server is None or server.kingdom_id != tenant.kingdom_id:
            raise HTTPException(status_code=422, detail="That server does not belong to this alliance's Kingdom")
    token = server.bot_token or PLATFORM_BOT_TOKEN
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No Discord bot token set for this tenant's server, and no platform fallback configured",
        )
    return DiscordCreds(bot_token=token, guild_id=server.guild_id)


async def check_target_access(db: AsyncSession, user: User, tenant_ids: list[int]):
    """A coordinator can only select tenants they hold UserTenant access
    to — matching the invite system's own access list, not a separately
    invented permission. Shared by Announcements' and Events' explicit
    multi-target features (spec §13.3/§20), which previously each had
    their own copy of this exact check."""
    if user.is_superadmin:
        return
    result = await db.execute(
        select(UserTenant.tenant_id).where(UserTenant.user_id == user.id, UserTenant.tenant_id.in_(tenant_ids))
    )
    accessible = {row[0] for row in result.all()}
    missing = set(tenant_ids) - accessible
    if missing:
        raise HTTPException(
            status_code=403,
            detail=f"You don't have access to tenant id(s) {sorted(missing)} to target them",
        )


async def resolve_target_tenants(db: AsyncSession, user: User, target_slugs: list[str]) -> dict[str, Tenant]:
    """Resolves a list of target tenant slugs to Tenant rows and checks
    access on all of them in one call — the two steps every multi-target
    create/update endpoint (Announcements, Events) needs before building
    its target rows. Returns {} for an empty list without hitting the DB."""
    if not target_slugs:
        return {}
    result = await db.execute(select(Tenant).where(Tenant.slug.in_(target_slugs)))
    tenants_by_slug = {t.slug: t for t in result.scalars().all()}
    missing = set(target_slugs) - set(tenants_by_slug.keys())
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown tenant slug(s): {sorted(missing)}")
    await check_target_access(db, user, [t.id for t in tenants_by_slug.values()])
    return tenants_by_slug


async def check_write_access(db: AsyncSession, user: User, tenant_ids: list[int]) -> None:
    """Like check_target_access, but also refuses a read-only Viewer grant. For
    actions that name several alliances in the body instead of one in the
    X-Tenant-Slug header (spec §79.6)."""
    if user.is_superadmin:
        return
    rows = (await db.execute(
        select(UserTenant.tenant_id, UserTenant.role).where(UserTenant.user_id == user.id, UserTenant.tenant_id.in_(tenant_ids))
    )).all()
    roles = {tenant_id: role for tenant_id, role in rows}
    missing = set(tenant_ids) - set(roles)
    if missing:
        raise HTTPException(status_code=403, detail=f"You don't have access to tenant id(s) {sorted(missing)}")
    if any(role == "viewer" for role in roles.values()):
        raise HTTPException(status_code=403, detail="Viewer access is read-only")
