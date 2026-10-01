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
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import get_db
from models.db import DiscordServer, EventType, Kingdom, Tenant, TenantSecondaryServer, User, UserTenant
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.discord_api import verify_token

from .deps import get_current_user, require_superadmin
from .schemas import (
    DiscordServerIn, DiscordServerPatch, KingdomIn, KingdomPatch, TenantIn, TenantPatch,
)

router = APIRouter()

# Every unique constraint below is an unnamed Column(unique=True) in
# models/db.py, so Postgres named it itself using its own default
# "<table>_<column>_key" convention (confirmed against the actual schema
# via app/alembic/versions/6fc935931248_..., not guessed) rather than an
# explicit `name=` this codebase chose. If a future migration ever renames
# one of these, raise_friendly_integrity_error's lookup just misses and
# falls back to its generic message — not a crash, just a less specific
# error text — but it's worth knowing why these particular strings look
# unlike this file's own explicit uq_* constraint names elsewhere.
_KINGDOM_SLUG_TAKEN = {"kingdoms_slug_key": "A kingdom with that slug already exists"}
_TENANT_SLUG_TAKEN = {"tenants_slug_key": "A tenant with that slug already exists"}
_GUILD_ID_TAKEN = {"discord_servers_guild_id_key": "A Discord server with that guild ID already exists"}


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
        "kingdom_id":        s.kingdom_id,
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
        "secondary_servers": [
            {"id": x.server_id, "name": x.server.name, "guild_id": x.server.guild_id}
            for x in sorted(t.secondary_servers, key=lambda r: r.server.name)
        ],
    }


async def _check_servers(db: AsyncSession, kingdom_id: int, primary_id: int, secondary_ids: list[int]) -> list[int]:
    """Spec §67.1: an alliance's primary and secondary servers must exist and
    belong to its own Kingdom, and a secondary cannot repeat the primary.
    Returns the secondary ids de-duplicated, in order."""
    unique: list[int] = []
    for sid in [primary_id, *secondary_ids]:
        server = await db.get(DiscordServer, sid)
        if not server:
            raise HTTPException(status_code=404, detail="Discord server not found")
        if server.kingdom_id != kingdom_id:
            raise HTTPException(status_code=422, detail=f'Server "{server.name}" belongs to a different Kingdom than this alliance')
        if sid != primary_id and sid not in unique:
            unique.append(sid)
    if primary_id in secondary_ids:
        raise HTTPException(status_code=422, detail="A secondary server cannot be the same as the primary server")
    return unique


async def _set_secondary_servers(db: AsyncSession, tenant: Tenant, server_ids: list[int]) -> None:
    await db.execute(delete(TenantSecondaryServer).where(TenantSecondaryServer.tenant_id == tenant.id))
    for sid in server_ids:
        db.add(TenantSecondaryServer(tenant_id=tenant.id, server_id=sid))
    await db.flush()
    await db.refresh(tenant, attribute_names=["secondary_servers"])


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
        await db.flush()
        # Every Kingdom starts with a default event type (spec §66.1).
        db.add(EventType(kingdom_id=kingdom.id, name="General"))
        await db.commit()
        await db.refresh(kingdom)
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _KINGDOM_SLUG_TAKEN, fallback="Could not create kingdom")
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
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _KINGDOM_SLUG_TAKEN, fallback="Could not update kingdom")
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
    secondary_ids = await _check_servers(db, payload.kingdom_id, payload.server_id, payload.secondary_server_ids)

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
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _TENANT_SLUG_TAKEN, fallback="Could not create tenant")
    await _set_secondary_servers(db, tenant, secondary_ids)

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

    new_primary = payload.server_id if payload.server_id is not None else tenant.server_id
    current_secondary = [r.server_id for r in tenant.secondary_servers]
    new_secondary = payload.secondary_server_ids if payload.secondary_server_ids is not None else current_secondary
    if payload.server_id is not None or payload.secondary_server_ids is not None:
        new_secondary = await _check_servers(db, tenant.kingdom_id, new_primary, new_secondary)
        if payload.secondary_server_ids is not None:
            await _set_secondary_servers(db, tenant, new_secondary)

    if payload.name is not None:      tenant.name      = payload.name
    if payload.slug is not None:      tenant.slug      = payload.slug
    if payload.server_id is not None: tenant.server_id = payload.server_id
    if payload.color is not None:     tenant.color     = payload.color
    if payload.icon_image_data is not None:
        tenant.icon_image_data = payload.icon_image_data or None

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="tenants", row_id=tenant.id, action="update",
        after={"name": tenant.name, "slug": tenant.slug, "server_id": tenant.server_id,
               "secondary_server_ids": [r.server_id for r in tenant.secondary_servers]},
    )
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        # Not previously caught at all — a slug collision on PATCH used to
        # surface as a bare 500 instead of the same clean 422 create_tenant
        # already gave for the identical constraint.
        raise_friendly_integrity_error(e, _TENANT_SLUG_TAKEN, fallback="Could not update tenant")
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

    if not await db.get(Kingdom, payload.kingdom_id):
        raise HTTPException(status_code=404, detail="Kingdom not found")

    server = DiscordServer(
        kingdom_id = payload.kingdom_id,
        name       = payload.name,
        guild_id   = payload.guild_id,
        bot_token  = payload.bot_token,
        public_key = payload.public_key,
    )
    db.add(server)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _GUILD_ID_TAKEN, fallback="Could not create Discord server")

    # bot_token/public_key deliberately excluded from the log entry — a
    # secret has no business sitting in a table other people can read.
    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="discord_servers", row_id=server.id, action="create",
        after={"name": server.name, "guild_id": server.guild_id, "kingdom_id": server.kingdom_id},
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

    if payload.kingdom_id is not None and payload.kingdom_id != server.kingdom_id:
        if not await db.get(Kingdom, payload.kingdom_id):
            raise HTTPException(status_code=404, detail="Kingdom not found")
        in_use = await db.execute(select(Tenant.name).where(Tenant.server_id == server.id, Tenant.kingdom_id != payload.kingdom_id))
        names = list(in_use.scalars().all())
        if names:
            raise HTTPException(status_code=422, detail=f"Alliances in another Kingdom use this server: {', '.join(names)}")
        server.kingdom_id = payload.kingdom_id
    if payload.name is not None:       server.name       = payload.name
    if payload.guild_id is not None:   server.guild_id   = payload.guild_id
    if payload.bot_token is not None:  server.bot_token  = payload.bot_token or None
    if payload.public_key is not None: server.public_key = payload.public_key or None

    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="discord_servers", row_id=server.id, action="update",
        after={"name": server.name, "guild_id": server.guild_id},
    )
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        # Same previously-uncaught gap as update_tenant above — a guild_id
        # collision on PATCH used to 500 instead of matching
        # create_discord_server's clean 422.
        raise_friendly_integrity_error(e, _GUILD_ID_TAKEN, fallback="Could not update Discord server")
    await db.refresh(server)
    names_result = await db.execute(select(Tenant.name).where(Tenant.server_id == server.id))
    return _server_dict(server, tenant_names=list(names_result.scalars().all()))
