from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import DiscordConfig, EventDefinition, Occurrence, PostLog
from services.discord_api import (
    cancel_discord_event, create_discord_event, send_channel_message,
)
from services.auth import require_admin_key

from .deps import find_post_log, get_discord_config, get_occurrence_with_event
from .schemas import OccurrencePatch
from .serializers import _occurrence_dict

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.get("/api/occurrences")
async def list_occurrences(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Occurrence, EventDefinition)
        .join(EventDefinition)
        .order_by(Occurrence.occurrence_date, EventDefinition.name)
    )
    return [_occurrence_dict(occ, ev) for occ, ev in result.all()]


@router.patch("/api/occurrences/{occ_id}")
async def update_occurrence(occ_id: int, payload: OccurrencePatch, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Occurrence).where(Occurrence.id == occ_id))
    occ    = result.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    if payload.post_to_discord is not None: occ.post_to_discord = payload.post_to_discord
    if payload.post_status is not None:     occ.post_status     = payload.post_status
    await db.commit()
    return {"id": occ.id, "post_to_discord": occ.post_to_discord, "post_status": occ.post_status}


@router.post("/api/occurrences/{occ_id}/post")
async def post_occurrence(
    db: AsyncSession = Depends(get_db),
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    cfg: DiscordConfig = Depends(get_discord_config),
):
    occ, event = occ_and_event

    now = datetime.now(timezone.utc)
    start = occ.start_datetime_utc
    if start.tzinfo is None:
        # Not every driver round-trips tzinfo on a DateTime(timezone=True)
        # column the same way — asyncpg (production/Postgres) does,
        # aiosqlite doesn't. Every value in this column is UTC regardless
        # of what the driver hands back, so normalise rather than assume.
        start = start.replace(tzinfo=timezone.utc)
    if (start - now).total_seconds() < 900:
        raise HTTPException(status_code=400, detail="Event starts in less than 15 minutes")

    if await find_post_log(db, event.name, occ.occurrence_date):
        raise HTTPException(status_code=409, detail="Already posted — see PostLog")

    # Reserve the PostLog row now, before calling Discord, instead of after.
    # Two concurrent POSTs for the same occurrence could otherwise both pass
    # the check above, both create a Discord event, and only then collide on
    # uq_post_log — by which point the second request's Discord event has
    # already been created with no PostLog row to record it. Inserting the
    # reservation first means the race is caught by the unique constraint
    # before any Discord call happens.
    log = PostLog(
        event_id         = event.id,
        event_name       = event.name,
        occurrence_date  = occ.occurrence_date,
        discord_guild_id = cfg.guild_id,
        posted_by        = "coordinator",
        status           = "pending",
    )
    db.add(log)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Already posted — see PostLog")

    occ.post_status = "queued"
    await db.commit()

    discord_id, error = await create_discord_event(
        token       = cfg.bot_token,
        guild_id    = cfg.guild_id,
        name        = event.name,
        start       = occ.start_datetime_utc,
        end         = occ.end_datetime_utc,
        description = event.description,
        location    = event.discord_channel,
    )

    if error:
        occ.post_status   = "error"
        occ.status_detail = error
        log.status        = "error"
        log.status_detail = error
        await db.commit()
        raise HTTPException(status_code=502, detail=error)

    log.discord_event_id = discord_id
    log.posted_at_utc    = datetime.now(timezone.utc)
    log.status           = "posted"
    occ.post_status       = "posted"
    occ.status_detail     = None

    # Send creation announcement if notification channel configured
    if event.notification_channel_id and event.notification_role_id:
        role_mention = f"<@&{event.notification_role_id}> "
        time_str     = occ.start_datetime_utc.strftime("%H:%M UTC")
        date_str     = occ.occurrence_date.strftime("%A %d %b")
        guild_id     = cfg.guild_id
        announce_msg = (
            f"{role_mention}📅 **{event.name}** has been scheduled\n"
            f"{date_str} · {time_str}"
            + (f" · {event.discord_channel}" if event.discord_channel else "")
            + (f"\nhttps://discord.com/events/{guild_id}/{discord_id}" if discord_id else "")
            + "\n\nClick **Interested** to get a reminder 30 minutes before."
        )
        await send_channel_message(cfg.bot_token, event.notification_channel_id, announce_msg)

    await db.commit()
    return {"discord_event_id": discord_id, "status": "posted"}


@router.delete("/api/occurrences/{occ_id}/discord", status_code=200)
async def cancel_occurrence_discord(
    db: AsyncSession = Depends(get_db),
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    cfg: DiscordConfig = Depends(get_discord_config),
):
    occ, event = occ_and_event

    log = await find_post_log(db, event.name, occ.occurrence_date)
    if not log or not log.discord_event_id:
        raise HTTPException(status_code=404, detail="No Discord event ID found in PostLog")

    success, error = await cancel_discord_event(cfg.bot_token, cfg.guild_id, log.discord_event_id)

    # discord_event_id keeps the real Discord ID rather than being
    # overwritten with a "CANCELLED — was {id}" marker string — status
    # already carries the cancelled state, and mangling the ID field broke
    # any later webhook lookup by that same ID (see webhooks.py).
    log.status = "cancelled"
    occ.post_status = "cancelled"
    await db.commit()

    if not success and "404" not in error:
        raise HTTPException(status_code=502, detail=error)
    return {"status": "cancelled", "discord_event_id": log.discord_event_id}

