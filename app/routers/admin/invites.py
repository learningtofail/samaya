"""Invite creation/listing/revocation — the only path to granting
UserTenant or UserKingdom access (see models.db.Invite and
routers/auth.py's claim flow).

Tenant invites (owner/coordinator) are created by that tenant's own
owner — self-service, no superadmin needed, matching how this was
scoped in the multi-tenant design doc. Kingdom-coordinator invites are
superadmin-only, since that grant crosses alliance boundaries within a
kingdom rather than staying inside one tenant's own settings.
"""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Invite, Kingdom, Tenant, User
from services.audit import log_change

from .deps import get_current_user, require_superadmin, require_tenant_owner
from .schemas import KingdomInviteIn, TenantInviteIn

router = APIRouter()

INVITE_EXPIRY_DAYS = 7


def _invite_dict(inv: Invite) -> dict:
    return {
        "id":          inv.id,
        "token":       inv.token,
        "tenant_id":   inv.tenant_id,
        "kingdom_id":  inv.kingdom_id,
        "role":        inv.role,
        "expires_at":  inv.expires_at.isoformat(),
        "used_at":     inv.used_at.isoformat() if inv.used_at else None,
        "used_by":     inv.used_by,
        "revoked_at":  inv.revoked_at.isoformat() if inv.revoked_at else None,
        "created_at":  inv.created_at.isoformat() if inv.created_at else None,
    }


@router.get("/api/invites")
async def list_tenant_invites(
    tenant: Tenant = Depends(require_tenant_owner), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Invite).where(Invite.tenant_id == tenant.id).order_by(Invite.created_at.desc())
    )
    return [_invite_dict(i) for i in result.scalars().all()]


@router.post("/api/invites", status_code=201)
async def create_tenant_invite(
    payload: TenantInviteIn,
    tenant: Tenant = Depends(require_tenant_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    invite = Invite(
        token=secrets.token_urlsafe(24),
        tenant_id=tenant.id,
        role=payload.role,
        created_by=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_EXPIRY_DAYS),
    )
    db.add(invite)
    await db.flush()
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="invites", row_id=invite.id, action="create",
        after={"tenant_id": tenant.id, "role": payload.role},
    )
    await db.commit()
    await db.refresh(invite)
    return _invite_dict(invite)


@router.delete("/api/invites/{invite_id}", status_code=200)
async def revoke_tenant_invite(
    invite_id: int,
    tenant: Tenant = Depends(require_tenant_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Invite).where(Invite.id == invite_id, Invite.tenant_id == tenant.id)
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.used_at:
        raise HTTPException(status_code=400, detail="Invite already claimed — nothing to revoke")

    invite.revoked_at = datetime.now(timezone.utc)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="invites", row_id=invite.id, action="update",
        before={"revoked_at": None}, after={"revoked_at": invite.revoked_at.isoformat()},
    )
    await db.commit()
    return {"status": "revoked"}


@router.get("/api/kingdom-invites")
async def list_kingdom_invites(
    kingdom_id: int, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Invite).where(Invite.kingdom_id == kingdom_id).order_by(Invite.created_at.desc())
    )
    return [_invite_dict(i) for i in result.scalars().all()]


@router.post("/api/kingdom-invites", status_code=201)
async def create_kingdom_invite(
    payload: KingdomInviteIn, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    kingdom = await db.get(Kingdom, payload.kingdom_id)
    if not kingdom:
        raise HTTPException(status_code=404, detail="Kingdom not found")

    invite = Invite(
        token=secrets.token_urlsafe(24),
        kingdom_id=kingdom.id,
        role="kingdom_coordinator",
        created_by=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_EXPIRY_DAYS),
    )
    db.add(invite)
    await db.flush()
    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="invites", row_id=invite.id, action="create",
        after={"kingdom_id": kingdom.id, "role": "kingdom_coordinator"},
    )
    await db.commit()
    await db.refresh(invite)
    return _invite_dict(invite)


@router.delete("/api/kingdom-invites/{invite_id}", status_code=200)
async def revoke_kingdom_invite(
    invite_id: int, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Invite).where(Invite.id == invite_id, Invite.kingdom_id.is_not(None)))
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.used_at:
        raise HTTPException(status_code=400, detail="Invite already claimed — nothing to revoke")

    invite.revoked_at = datetime.now(timezone.utc)
    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="invites", row_id=invite.id, action="update",
        before={"revoked_at": None}, after={"revoked_at": invite.revoked_at.isoformat()},
    )
    await db.commit()
    return {"status": "revoked"}
