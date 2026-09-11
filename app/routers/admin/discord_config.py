from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import DiscordConfig
from services.discord_api import get_guild_channels, get_guild_roles, verify_token
from services.auth import require_admin_key

from .schemas import DiscordConfigIn

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.put("/api/config/discord")
async def update_discord_config(payload: DiscordConfigIn, db: AsyncSession = Depends(get_db)):
    ok, result = await verify_token(payload.bot_token)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Token verification failed: {result}")
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(DiscordConfig).values(
        id=1, bot_token=payload.bot_token, guild_id=payload.guild_id,
        public_key=payload.public_key, updated_by="coordinator",
    ).on_conflict_do_update(
        index_elements=["id"],
        set_={"bot_token": payload.bot_token, "guild_id": payload.guild_id,
              "public_key": payload.public_key, "updated_by": "coordinator"}
    )
    await db.execute(stmt)
    await db.commit()
    return {"status": "ok", "bot_username": result}


@router.get("/api/discord/channels")
async def list_discord_channels(db: AsyncSession = Depends(get_db)):
    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")
    channels, error = await get_guild_channels(cfg.bot_token, cfg.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return channels


@router.get("/api/discord/roles")
async def list_discord_roles(db: AsyncSession = Depends(get_db)):
    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")
    roles, error = await get_guild_roles(cfg.bot_token, cfg.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return roles

