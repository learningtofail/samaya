"""Shared lookups used by several admin routers.

Extracted after the routers/admin.py -> routers/admin/ split made the
duplication visible: the same "load DiscordConfig or 400" and "load
Occurrence+EventDefinition or 404" blocks were copy-pasted across
occurrences.py, discord_sync.py, and discord_config.py. Fixing the message
or status code for either now happens once instead of eight times.
"""
from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import DiscordConfig, EventDefinition, Occurrence, PostLog


async def get_discord_config(db: AsyncSession = Depends(get_db)) -> DiscordConfig:
    result = await db.execute(select(DiscordConfig))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")
    return cfg


async def get_occurrence_with_event(
    occ_id: int, db: AsyncSession = Depends(get_db)
) -> tuple[Occurrence, EventDefinition]:
    result = await db.execute(
        select(Occurrence, EventDefinition).join(EventDefinition).where(Occurrence.id == occ_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    return row


async def find_post_log(db: AsyncSession, event_name: str, occurrence_date) -> PostLog | None:
    """Not a FastAPI dependency — callers need different behavior when no
    row is found (some treat it as fine, some as a 404), so this stays a
    plain helper rather than one more Depends with baked-in error handling."""
    result = await db.execute(
        select(PostLog).where(
            PostLog.event_name == event_name,
            PostLog.occurrence_date == occurrence_date,
        )
    )
    return result.scalar_one_or_none()
