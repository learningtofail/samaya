"""Kingdom and Tenant management — the "onboard a new alliance" surface.

Creating/editing Kingdoms and Tenants is superadmin-only (require_superadmin).
Listing tenants is scoped to what the logged-in user actually has access
to — a superadmin sees every tenant (needed to onboard new alliances and
pick their first owner); anyone else sees only the tenants their own
UserTenant grants cover, since this list also populates the admin UI's
tenant picker and a coordinator has no business seeing (or switching
into) alliances they don't belong to.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import get_db
from models.db import DiscordServer, Kingdom, Tenant, User, UserTenant
from services.audit import log_change
from services.discord_api import verify_token

from .deps import get_current_user, require_superadmin
from .schemas import DiscordServerIn, DiscordServerPatch, KingdomIn, KingdomPatch, TenantIn, TenantPatch

router = APIRouter()


def _kingdom_dict(k: Kingdom) -> dict:
    return {
        "id": k.id, "name": k.name, "slug": k.slug,
        "public_site_title":   k.public_site_title,
        "admin_console_title": k.admin_console_title,
    }


def _server_dict(s: DiscordServer, tenant_names: list[str] | None = None) -> dict:
    """`tenant_names` must be passed explicitly wherever `s.tenants` isn't
    already eager-loaded on this object (e.g. right after create/update) —
    accessing an unloaded relationship here would attempt a lazy load,
    which fails under the async engine (see spec §25.2's MissingGreenlet
    note). list_discord_servers below eager-loads it and can omit this."""
    return {
        "id":                s.id,
        "name":              s.name,
        "guild_id":          s.guild_id,
        "has_own_bot_token": bool(s.bot_token),
        "tenant_names":      tenant_names if tenant_names is not None else [t.name for t in s.tenants],
    }


def _tenant_dict(t: Tenant) -> dict:
    return {
        "id":          t.id,
        "kingdom_id":  t.kingdom_id,
        "name":        t.name,
        "slug":        t.slug,
        "server_id":   t.server_id,
        "server_name": t.server.name,
        "guild_id":    t.server.guild_id,
        "color":       t.color,
        "icon_image_data": t.icon_image_data or None,
    }


@router.get("/api/kingdoms")
async def list_kingdoms(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Kingdom).order_by(Kingdom.name))
    return [_kingdom_dict(k) for k in result.scalars().all()]


@router.post("/api/kingdoms", status_code=201)
async def create_kingdom(
    payload: KingdomIn, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    kingdom = Kingdom(name=payload.name, slug=payload.slug)
    db.add(kingdom)
    try:
        await db.commit()
        await db.refresh(kingdom)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not create kingdom: {e}")
    return _kingdom_dict(kingdom)


@router.patch("/api/kingdoms/{kingdom_id}")
async def update_kingdom(
    kingdom_id: int, payload: KingdomPatch,
    user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db),
):
    kingdom = await db.get(Kingdom, kingdom_id)
    if not kingdom:
        raise HTTPException(status_code=404, detail="Kingdom not found")

    before = {"name": kingdom.name, "slug": kingdom.slug}
    if payload.name is not None: kingdom.name = payload.name
    if payload.slug is not None: kingdom.slug = payload.slug
    if payload.public_site_title is not None:
        kingdom.public_site_title = payload.public_site_title or None
    if payload.admin_console_title is not None:
        kingdom.admin_console_title = payload.admin_console_title or None

    try:
        await log_change(
            db, user_id=user.id, tenant_id=None,
            table_name="kingdoms", row_id=kingdom.id, action="update",
            before=before, after={"name": kingdom.name, "slug": kingdom.slug},
        )
        await db.commit()
        await db.refresh(kingdom)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not update kingdom: {e}")
    return _kingdom_dict(kingdom)


@router.get("/api/tenants")
async def list_tenants(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user.is_superadmin:
        result = await db.execute(select(Tenant).order_by(Tenant.name))
    else:
        result = await db.execute(
            select(Tenant).join(UserTenant, UserTenant.tenant_id == Tenant.id)
            .where(UserTenant.user_id == user.id)
            .order_by(Tenant.name)
        )
    return [_tenant_dict(t) for t in result.scalars().all()]


@router.post("/api/tenants", status_code=201)
async def create_tenant(
    payload: TenantIn, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    server = await db.get(DiscordServer, payload.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Discord server not found")

    tenant = Tenant(
        kingdom_id = payload.kingdom_id,
        name       = payload.name,
        slug       = payload.slug,
        server_id  = payload.server_id,
        color      = payload.color,
        icon_image_data = payload.icon_image_data or None,
    )
    db.add(tenant)
    try:
        await db.flush()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not create tenant: {e}")

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="tenants", row_id=tenant.id, action="create",
        after={"name": tenant.name, "slug": tenant.slug, "kingdom_id": tenant.kingdom_id, "server_id": tenant.server_id},
    )
    await db.commit()
    # A plain refresh() wouldn't populate the `server` relationship (it
    # was never loaded on this brand-new object) — re-select instead,
    # which picks up Tenant.server's lazy="joined" default.
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
    return _tenant_dict(tenant)


@router.patch("/api/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: int, payload: TenantPatch,
    user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if payload.server_id is not None:
        server = await db.get(DiscordServer, payload.server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Discord server not found")

    if payload.name is not None:      tenant.name      = payload.name
    if payload.slug is not None:      tenant.slug      = payload.slug
    if payload.server_id is not None: tenant.server_id = payload.server_id
    if payload.color is not None:     tenant.color     = payload.color
    if payload.icon_image_data is not None:
        tenant.icon_image_data = payload.icon_image_data or None

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="tenants", row_id=tenant.id, action="update",
        after={"name": tenant.name, "slug": tenant.slug, "server_id": tenant.server_id},
    )
    await db.commit()
    # `tenant` was already fetched (with `.server` eager-loaded) at the top
    # of this function, so a plain re-select would return the same
    # identity-mapped object with its now-stale `.server` still attached —
    # a re-select only populates attributes that were never loaded (that's
    # why create_tenant's version above works). Explicitly refresh the
    # relationship instead, whenever server_id itself just changed.
    if payload.server_id is not None:
        await db.refresh(tenant, attribute_names=["server"])
    return _tenant_dict(tenant)


@router.get("/api/discord-servers")
async def list_discord_servers(user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    """Superadmin-only, same tier as Kingdoms/Tenants/Users (spec §25.3).
    A coordinator/owner never needs this list directly — their access to
    a server's channels/roles flows through their Tenant, via
    get_discord_config (routers/admin/deps.py), not through this table."""
    result = await db.execute(
        select(DiscordServer).options(selectinload(DiscordServer.tenants)).order_by(DiscordServer.name)
    )
    return [_server_dict(s) for s in result.scalars().all()]


@router.post("/api/discord-servers", status_code=201)
async def create_discord_server(
    payload: DiscordServerIn, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    if payload.bot_token:
        ok, result = await verify_token(payload.bot_token)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Bot token verification failed: {result}")

    server = DiscordServer(
        name       = payload.name,
        guild_id   = payload.guild_id,
        bot_token  = payload.bot_token,
        public_key = payload.public_key,
    )
    db.add(server)
    try:
        await db.flush()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not create Discord server: {e}")

    # bot_token/public_key deliberately excluded from the log entry — a
    # secret has no business sitting in a table other people can read.
    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="discord_servers", row_id=server.id, action="create",
        after={"name": server.name, "guild_id": server.guild_id},
    )
    await db.commit()
    await db.refresh(server)
    return _server_dict(server, tenant_names=[])  # brand new — nothing references it yet


@router.patch("/api/discord-servers/{server_id}")
async def update_discord_server(
    server_id: int, payload: DiscordServerPatch,
    user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db),
):
    server = await db.get(DiscordServer, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Discord server not found")

    if payload.bot_token is not None and payload.bot_token != "":
        ok, verify_result = await verify_token(payload.bot_token)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Bot token verification failed: {verify_result}")

    if payload.name is not None:       server.name       = payload.name
    if payload.guild_id is not None:   server.guild_id   = payload.guild_id
    if payload.bot_token is not None:  server.bot_token  = payload.bot_token or None
    if payload.public_key is not None: server.public_key = payload.public_key or None

    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="discord_servers", row_id=server.id, action="update",
        after={"name": server.name, "guild_id": server.guild_id},
    )
    await db.commit()
    await db.refresh(server)
    names_result = await db.execute(select(Tenant.name).where(Tenant.server_id == server.id))
    return _server_dict(server, tenant_names=list(names_result.scalars().all()))
