"""GET /admin/api/me — tells the frontend who's logged in and what they
can do, so it can show/hide the superadmin and owner-only UI without
every view having to guess from failed requests.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import User, UserTenant
from services.audit import log_change

from .deps import get_current_user
from .schemas import DisplayNameIn

router = APIRouter()


@router.get("/api/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserTenant).where(UserTenant.user_id == user.id))
    grants = result.scalars().all()
    return {
        "id":               user.id,
        "discord_username": user.discord_username,
        "display_name":     user.display_name,
        "is_superadmin":    user.is_superadmin,
        "tenant_roles":      {g.tenant_id: g.role for g in grants},
    }


@router.patch("/api/me")
async def update_me(payload: DisplayNameIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Sets the caller's own display name: what public feedback responses
    show as their author. Blank clears it, and replies then show as "Team"."""
    before = user.display_name
    user.display_name = payload.display_name.strip() or None
    await log_change(
        db, user_id=user.id, tenant_id=None, table_name="users", row_id=user.id, action="update",
        before={"display_name": before}, after={"display_name": user.display_name},
    )
    await db.commit()
    return {"id": user.id, "display_name": user.display_name}
