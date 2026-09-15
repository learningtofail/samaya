"""GET /admin/api/me — tells the frontend who's logged in and what they
can do, so it can show/hide the superadmin and owner-only UI without
every view having to guess from failed requests.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import User, UserTenant

from .deps import get_current_user

router = APIRouter()


@router.get("/api/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserTenant).where(UserTenant.user_id == user.id))
    grants = result.scalars().all()
    return {
        "id":               user.id,
        "discord_username": user.discord_username,
        "is_superadmin":    user.is_superadmin,
        "tenant_roles":      {g.tenant_id: g.role for g in grants},
    }
