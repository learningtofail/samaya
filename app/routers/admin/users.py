"""Platform-wide user listing and superadmin-flag management —
superadmin-only. Kept separate from invites.py (which manages per-tenant/
per-kingdom access grants): this operates on the User table itself, not
a grant, and its two routes cross every tenant/kingdom boundary rather
than being scoped to one.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import User
from services.audit import log_change

from .deps import require_superadmin
from .schemas import UserPatch

router = APIRouter()


def _user_dict(u: User) -> dict:
    return {
        "id":               u.id,
        "discord_id":       u.discord_id,
        "discord_username": u.discord_username,
        "is_superadmin":    u.is_superadmin,
        "created_at":       u.created_at.isoformat() if u.created_at else None,
        "last_login_at":    u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.get("/api/users")
async def list_users(user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.discord_username))
    return [_user_dict(u) for u in result.scalars().all()]


@router.patch("/api/users/{user_id}")
async def update_user(
    user_id: int, payload: UserPatch,
    user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # A superadmin revoking their own flag has no UI path back in — the
    # next request would 403 on require_superadmin with no one able to
    # undo it short of direct database access. Someone *else* can still
    # demote them.
    if target.id == user.id and not payload.is_superadmin:
        raise HTTPException(status_code=400, detail="You can't remove your own superadmin access")

    before = target.is_superadmin
    target.is_superadmin = payload.is_superadmin
    await log_change(
        db, user_id=user.id, tenant_id=None,
        table_name="users", row_id=target.id, action="update",
        before={"is_superadmin": before}, after={"is_superadmin": target.is_superadmin},
    )
    await db.commit()
    await db.refresh(target)
    return _user_dict(target)
