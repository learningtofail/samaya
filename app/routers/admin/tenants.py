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

from models import get_db
from models.db import Kingdom, Tenant, User, UserTenant
from services.audit import log_change
from services.discord_api import verify_token

from .deps import get_current_user, require_superadmin
from .schemas import KingdomIn, TenantIn, TenantPatch

router = APIRouter()


def _kingdom_dict(k: Kingdom) -> dict:
    return {"id": k.id, "name": k.name, "slug": k.slug}


def _tenant_dict(t: Tenant) -> dict:
    return {
        "id":         t.id,
        "kingdom_id": t.kingdom_id,
        "name":       t.name,
        "slug":       t.slug,
        "guild_id":   t.guild_id,
        "has_own_bot_token": bool(t.bot_token),
        "color":      t.color,
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
    if payload.bot_token:
        ok, result = await verify_token(payload.bot_token)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Bot token verification failed: {result}")

    tenant = Tenant(
        kingdom_id = payload.kingdom_id,
        name       = payload.name,
        slug       = payload.slug,
        guild_id   = payload.guild_id,
        bot_token  = payload.bot_token,
        public_key = payload.public_key,
        color      = payload.color,
    )
    db.add(tenant)
    try:
        await db.flush()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not create tenant: {e}")

    # bot_token/public_key deliberately excluded from the log entry — a
    # secret has no business sitting in a table other people can read.
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="tenants", row_id=tenant.id, action="create",
        after={"name": tenant.name, "slug": tenant.slug, "kingdom_id": tenant.kingdom_id},
    )
    await db.commit()
    await db.refresh(tenant)
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

    if payload.bot_token is not None and payload.bot_token != "":
        ok, verify_result = await verify_token(payload.bot_token)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Bot token verification failed: {verify_result}")

    if payload.name is not None:       tenant.name       = payload.name
    if payload.slug is not None:       tenant.slug       = payload.slug
    if payload.guild_id is not None:   tenant.guild_id   = payload.guild_id
    if payload.bot_token is not None:  tenant.bot_token  = payload.bot_token or None
    if payload.public_key is not None: tenant.public_key = payload.public_key or None
    if payload.color is not None:      tenant.color      = payload.color

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="tenants", row_id=tenant.id, action="update",
        after={"name": tenant.name, "slug": tenant.slug, "guild_id": tenant.guild_id},
    )
    await db.commit()
    await db.refresh(tenant)
    return _tenant_dict(tenant)
