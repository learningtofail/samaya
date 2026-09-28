"""Invite creation/listing/revocation — the only path to granting
UserTenant or UserKingdom access (see models.db.Invite and
routers/auth.py's claim flow) — plus managing grants that already exist,
once an invite has been claimed. The two are kept in one file because
they're the same access model viewed at two points in its lifecycle:
invites.py owns everything about who has, or is about to get, standing
access to a tenant or kingdom.

Tenant invites (owner/coordinator) are created by that tenant's own
owner — self-service, no superadmin needed, matching how this was
scoped in the multi-tenant design doc. Kingdom-coordinator invites are
superadmin-only, since that grant crosses alliance boundaries within a
kingdom rather than staying inside one tenant's own settings. The same
split applies to editing/removing an already-claimed grant: a tenant
owner manages their own tenant's UserTenant rows, a superadmin manages
UserKingdom rows.
"""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Invite, Kingdom, Tenant, User, UserKingdom, UserTenant
from services.audit import log_change

from .deps import get_current_user, require_superadmin, require_tenant_owner
from .schemas import KingdomInviteIn, TenantInviteIn, UserTenantPatch

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


def _member_dict(ut: UserTenant, user: User) -> dict:
    return {
        "id":               ut.id,
        "user_id":          user.id,
        "discord_id":       user.discord_id,
        "discord_username": user.discord_username,
        "role":             ut.role,
    }


async def _sole_owner(db: AsyncSession, tenant_id: int) -> bool:
    """True if tenant_id has exactly one owner left — used to block a
    demote/remove that would leave the alliance with no one able to
    invite, edit config, or manage access at all."""
    result = await db.execute(
        select(func.count()).select_from(UserTenant)
        .where(UserTenant.tenant_id == tenant_id, UserTenant.role == "owner")
    )
    return result.scalar_one() <= 1


@router.get("/api/members")
async def list_tenant_members(
    tenant: Tenant = Depends(require_tenant_owner), db: AsyncSession = Depends(get_db)
):
    """Everyone with standing (already-claimed) access to the current
    tenant — the counterpart to /api/invites, which only shows pending/
    used/revoked invite records, not the resulting grants."""
    result = await db.execute(
        select(UserTenant, User).join(User, User.id == UserTenant.user_id)
        .where(UserTenant.tenant_id == tenant.id)
        .order_by(User.discord_username)
    )
    return [_member_dict(ut, u) for ut, u in result.all()]


@router.patch("/api/members/{member_id}")
async def update_tenant_member(
    member_id: int, payload: UserTenantPatch,
    tenant: Tenant = Depends(require_tenant_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserTenant).where(UserTenant.id == member_id, UserTenant.tenant_id == tenant.id)
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    if member.role == "owner" and payload.role != "owner" and await _sole_owner(db, tenant.id):
        raise HTTPException(
            status_code=400,
            detail="Can't demote the only owner — promote someone else to owner first",
        )

    before_role = member.role
    member.role = payload.role
    target_user = await db.get(User, member.user_id)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="user_tenants", row_id=member.id, action="update",
        before={"role": before_role}, after={"role": member.role},
    )
    await db.commit()
    await db.refresh(member)
    return _member_dict(member, target_user)


@router.delete("/api/members/{member_id}", status_code=200)
async def remove_tenant_member(
    member_id: int,
    tenant: Tenant = Depends(require_tenant_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserTenant).where(UserTenant.id == member_id, UserTenant.tenant_id == tenant.id)
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    if member.role == "owner" and await _sole_owner(db, tenant.id):
        raise HTTPException(
            status_code=400,
            detail="Can't remove the only owner — promote someone else to owner first",
        )

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="user_tenants", row_id=member.id, action="delete",
        before={"user_id": member.user_id, "role": member.role},
    )
    await db.delete(member)
    await db.commit()
    return {"status": "removed"}


@router.get("/api/kingdom-coordinators")
async def list_kingdom_coordinators(
    kingdom_id: int, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    """The standing counterpart to /api/kingdom-invites — everyone who
    has actually claimed kingdom-coordinator access, not just pending
    invites for it."""
    result = await db.execute(
        select(UserKingdom, User).join(User, User.id == UserKingdom.user_id)
        .where(UserKingdom.kingdom_id == kingdom_id)
        .order_by(User.discord_username)
    )
    return [
        {"id": uk.id, "user_id": u.id, "discord_id": u.discord_id, "discord_username": u.discord_username}
        for uk, u in result.all()
    ]


@router.delete("/api/kingdom-coordinators/{grant_id}", status_code=200)
async def remove_kingdom_coordinator(
    grant_id: int, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    grant = await db.get(UserKingdom, grant_id)
    if not grant:
        raise HTTPException(status_code=404, detail="Kingdom coordinator grant not found")

    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="user_kingdoms", row_id=grant.id, action="delete",
        before={"user_id": grant.user_id, "kingdom_id": grant.kingdom_id},
    )
    await db.delete(grant)
    await db.commit()
    return {"status": "removed"}


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
