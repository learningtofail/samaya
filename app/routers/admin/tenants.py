"""Kingdom and Tenant management — the "onboard a new alliance" surface.

Gated by require_admin_key only, same as every other admin route, for now.
Per the multi-tenant design doc, this becomes superadmin-only once Phase 4
(real auth) lands — creating tenants is not itself tenant-scoped, so it
deliberately does not depend on get_current_tenant the way every other
admin router does.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Kingdom, Tenant
from services.auth import require_admin_key
from services.discord_api import verify_token

from .schemas import KingdomIn, TenantIn, TenantPatch

router = APIRouter(dependencies=[Depends(require_admin_key)])


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
async def list_kingdoms(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Kingdom).order_by(Kingdom.name))
    return [_kingdom_dict(k) for k in result.scalars().all()]


@router.post("/api/kingdoms", status_code=201)
async def create_kingdom(payload: KingdomIn, db: AsyncSession = Depends(get_db)):
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
async def list_tenants(db: AsyncSession = Depends(get_db)):
    """Every tenant, not just the caller's own — this is what populates the
    tenant picker in the admin UI, so it deliberately isn't scoped to one
    tenant. Bridge-period trust model: anyone with the shared admin key can
    see (and switch into) every alliance. Phase 4 restricts this to the
    tenants a logged-in user actually has UserTenant access to."""
    result = await db.execute(select(Tenant).order_by(Tenant.name))
    return [_tenant_dict(t) for t in result.scalars().all()]


@router.post("/api/tenants", status_code=201)
async def create_tenant(payload: TenantIn, db: AsyncSession = Depends(get_db)):
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
        await db.commit()
        await db.refresh(tenant)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not create tenant: {e}")
    return _tenant_dict(tenant)


@router.patch("/api/tenants/{tenant_id}")
async def update_tenant(tenant_id: int, payload: TenantPatch, db: AsyncSession = Depends(get_db)):
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

    await db.commit()
    await db.refresh(tenant)
    return _tenant_dict(tenant)
