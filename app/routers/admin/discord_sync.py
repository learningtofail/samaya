from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import DiscordConfig, EventDefinition, PostLog
from services.discord_api import update_discord_event
from services.auth import require_admin_key

from .deps import find_post_log, get_discord_config, get_occurrence_with_event

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.get("/api/sync/discord")
async def sync_discord(
    db: AsyncSession = Depends(get_db),
    cfg: DiscordConfig = Depends(get_discord_config),
):
    """
    Fetches all current Discord scheduled events and compares
    against PostLog. Returns four categories:
    - matched: in both, details align
    - mismatched: in both, but fields differ from event definition
    - discord_only: on Discord but not in PostLog
    - postlog_only: in PostLog as posted but not found on Discord
    """
    from services.discord_api import get_guild_events
    discord_events = await get_guild_events(cfg.bot_token, cfg.guild_id)

    # Build lookup maps
    discord_map = {str(e["id"]): e for e in discord_events}

    # Load all posted PostLog entries
    log_result = await db.execute(
        select(PostLog).where(PostLog.status == "posted")
    )
    post_logs = log_result.scalars().all()

    # Load event definitions for comparison
    ev_result = await db.execute(select(EventDefinition))
    event_defs = {e.id: e for e in ev_result.scalars().all()}

    matched      = []
    mismatched   = []
    postlog_only = []

    postlog_discord_ids = set()

    for log in post_logs:
        if not log.discord_event_id:
            continue

        discord_id = str(log.discord_event_id)
        postlog_discord_ids.add(discord_id)

        if discord_id not in discord_map:
            # Posted in PostLog but not found on Discord
            postlog_only.append({
                "post_log_id":      log.id,
                "event_name":       log.event_name,
                "occurrence_date":  str(log.occurrence_date),
                "discord_event_id": discord_id,
                "posted_at_utc":    log.posted_at_utc.isoformat() if log.posted_at_utc else None,
            })
            continue

        d_event = discord_map[discord_id]

        # Compare fields against event definition
        diffs = []
        if log.event_id and log.event_id in event_defs:
            ev = event_defs[log.event_id]
            if d_event.get("name") != log.event_name:
                diffs.append({
                    "field":    "name",
                    "samaya":   log.event_name,
                    "discord":  d_event.get("name"),
                })
            if d_event.get("description", "") != (ev.description or ""):
                diffs.append({
                    "field":    "description",
                    "samaya":   ev.description,
                    "discord":  d_event.get("description", ""),
                })
            # Status check
            d_status = d_event.get("status")
            # Discord status: 1=SCHEDULED 2=ACTIVE 3=COMPLETED 4=CANCELLED
            if d_status == 4:
                diffs.append({
                    "field":    "status",
                    "samaya":   "posted",
                    "discord":  "CANCELLED",
                })

        entry = {
            "post_log_id":      log.id,
            "event_name":       log.event_name,
            "occurrence_date":  str(log.occurrence_date),
            "discord_event_id": discord_id,
            "discord_name":     d_event.get("name"),
            "discord_status":   d_event.get("status"),
            "diffs":            diffs,
        }

        if diffs:
            mismatched.append(entry)
        else:
            matched.append(entry)

    # Discord-only: on Discord but not in any PostLog entry
    discord_only = []
    for discord_id, d_event in discord_map.items():
        if discord_id not in postlog_discord_ids:
            discord_only.append({
                "discord_event_id": discord_id,
                "name":             d_event.get("name"),
                "status":           d_event.get("status"),
                "scheduled_start":  d_event.get("scheduled_start_time"),
            })

    return {
        "summary": {
            "matched":       len(matched),
            "mismatched":    len(mismatched),
            "discord_only":  len(discord_only),
            "postlog_only":  len(postlog_only),
            "issues":        len(mismatched) + len(postlog_only),
        },
        "matched":      matched,
        "mismatched":   mismatched,
        "discord_only": discord_only,
        "postlog_only": postlog_only,
    }


@router.post("/api/sync/push/{occ_id}")
async def sync_push_to_discord(
    db: AsyncSession = Depends(get_db),
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    cfg: DiscordConfig = Depends(get_discord_config),
):
    """
    Updates a Discord event to match the current event definition.
    Uses PATCH /guilds/{guild_id}/scheduled-events/{event_id}.
    """
    occ, event = occ_and_event

    log = await find_post_log(db, event.name, occ.occurrence_date)
    if not log or not log.discord_event_id:
        raise HTTPException(status_code=404, detail="No Discord event ID in PostLog")

    success, error = await update_discord_event(
        token             = cfg.bot_token,
        guild_id          = cfg.guild_id,
        discord_event_id  = log.discord_event_id,
        name              = event.name,
        description       = event.description,
        location          = event.discord_channel,
    )

    if success:
        return {"status": "ok", "message": f"Discord event updated for {event.name} on {occ.occurrence_date}"}
    raise HTTPException(status_code=502, detail=f"Discord PATCH failed: {error}")


@router.post("/api/sync/acknowledge/{discord_event_id}")
async def acknowledge_discord_event(
    discord_event_id: str,
    db: AsyncSession = Depends(get_db),
    cfg: DiscordConfig = Depends(get_discord_config),
):
    """
    Adds a Discord-only event to PostLog as a manually-created record.
    """
    from services.discord_api import get_guild_events
    discord_events = await get_guild_events(cfg.bot_token, cfg.guild_id)
    d_event = next((e for e in discord_events if str(e["id"]) == discord_event_id), None)
    if not d_event:
        raise HTTPException(status_code=404, detail="Discord event not found")

    # Find a matching event definition by name
    ev_result = await db.execute(
        select(EventDefinition).where(EventDefinition.name == d_event.get("name"))
    )
    ev = ev_result.scalar_one_or_none()

    from datetime import datetime, timezone
    start_str = d_event.get("scheduled_start_time", "")
    try:
        occ_date = datetime.fromisoformat(start_str.replace("Z", "+00:00")).date()
    except Exception:
        occ_date = datetime.now(timezone.utc).date()

    log = PostLog(
        event_id         = ev.id if ev else None,
        event_name       = d_event.get("name", "Unknown"),
        occurrence_date  = occ_date,
        discord_event_id = discord_event_id,
        discord_guild_id = cfg.guild_id,
        posted_at_utc    = datetime.now(timezone.utc),
        posted_by        = "manual (acknowledged via sync)",
        status           = "posted",
        status_detail    = "Acknowledged from Discord sync — created outside Samaya",
    )
    db.add(log)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Could not acknowledge: {str(e)}")

    return {"status": "ok", "message": f"Event acknowledged and added to PostLog"}


@router.post("/api/sync/push-by-log/{post_log_id}")
async def sync_push_by_log(
    post_log_id: int,
    db: AsyncSession = Depends(get_db),
    cfg: DiscordConfig = Depends(get_discord_config),
):
    """Push Samaya values to Discord using PostLog ID."""
    log_result = await db.execute(select(PostLog).where(PostLog.id == post_log_id))
    log = log_result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="PostLog entry not found")

    ev_result = await db.execute(
        select(EventDefinition).where(EventDefinition.id == log.event_id)
    )
    event = ev_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event definition not found")

    success, error = await update_discord_event(
        token             = cfg.bot_token,
        guild_id          = cfg.guild_id,
        discord_event_id  = log.discord_event_id,
        name              = event.name,
        description       = event.description,
        location          = event.discord_channel,
    )
    if success:
        return {"status": "ok"}
    raise HTTPException(status_code=502, detail=f"Discord PATCH failed: {error}")


@router.post("/api/sync/mark-cancelled/{post_log_id}")
async def sync_mark_cancelled(post_log_id: int, db: AsyncSession = Depends(get_db)):
    """Mark a PostLog entry as cancelled when Discord event no longer exists."""
    log_result = await db.execute(select(PostLog).where(PostLog.id == post_log_id))
    log = log_result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="PostLog entry not found")
    log.status        = "cancelled"
    log.status_detail = "Marked cancelled via sync — Discord event not found"
    await db.commit()
    return {"status": "ok"}

