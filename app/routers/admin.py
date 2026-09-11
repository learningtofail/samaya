from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import (
    DiscordConfig, EventDefinition, Occurrence,
    PostLog, SchedulerState
)
from services.discord_api import (
    cancel_discord_event, create_discord_event, update_discord_event,
    get_guild_channels, get_guild_roles,
    send_channel_message, verify_token
)
from services.recurrence import next_occurrences, build_start_datetime
from services.validators import (
    parse_interval_days, parse_duration_hours, parse_alliance,
    parse_start_time_utc, parse_anchor_date, parse_notify_minutes_before,
)
from scheduler.jobs import regenerate_occurrences
from services.auth import require_admin_key

router = APIRouter()
api_router = APIRouter(dependencies=[Depends(require_admin_key)])


# ── Pydantic schemas ──────────────────────────────────────────

class EventIn(BaseModel):
    name:                    str
    interval_days:           int
    start_time_utc:          str
    duration_hours:          float
    discord_channel:         str = ""
    description:             str = ""
    category:                str = "Other"
    alliance:                str = "Server"
    leadership_only:         bool = False
    anchor_date:             str
    notification_channel_id: str = ""
    notification_role_id:    str = ""
    notify_minutes_before:   Optional[int] = None

    _validate_interval = field_validator("interval_days", mode="before")(parse_interval_days)
    _validate_duration = field_validator("duration_hours", mode="before")(parse_duration_hours)
    _validate_alliance = field_validator("alliance", mode="before")(parse_alliance)
    _validate_time     = field_validator("start_time_utc", mode="before")(parse_start_time_utc)
    _validate_anchor   = field_validator("anchor_date", mode="before")(parse_anchor_date)
    _validate_notify   = field_validator("notify_minutes_before", mode="before")(parse_notify_minutes_before)


class EventPatch(BaseModel):
    name:                    Optional[str]  = None
    interval_days:           Optional[int]  = None
    start_time_utc:          Optional[str]  = None
    duration_hours:          Optional[float]= None
    discord_channel:         Optional[str]  = None
    description:             Optional[str]  = None
    category:                Optional[str]  = None
    alliance:                Optional[str]  = None
    leadership_only:         Optional[bool] = None
    active:                  Optional[bool] = None
    anchor_date:             Optional[str]  = None
    notification_channel_id: Optional[str]  = None
    notification_role_id:    Optional[str]  = None
    notify_minutes_before:   Optional[int]  = None

    # Same parsing rules as EventIn, but None means "leave this field
    # unchanged" on a partial update rather than "invalid".
    _validate_interval = field_validator("interval_days", mode="before")(
        lambda cls, v: parse_interval_days(v, allow_none=True))
    _validate_duration = field_validator("duration_hours", mode="before")(
        lambda cls, v: parse_duration_hours(v, allow_none=True))
    _validate_alliance = field_validator("alliance", mode="before")(
        lambda cls, v: parse_alliance(v, allow_none=True))
    _validate_time     = field_validator("start_time_utc", mode="before")(
        lambda cls, v: parse_start_time_utc(v, allow_none=True))
    _validate_anchor   = field_validator("anchor_date", mode="before")(
        lambda cls, v: parse_anchor_date(v, allow_none=True))
    _validate_notify   = field_validator("notify_minutes_before", mode="before")(
        lambda cls, v: parse_notify_minutes_before(v, allow_none=True))

class DiscordConfigIn(BaseModel):
    bot_token:  str
    guild_id:   str
    public_key: str

class OccurrencePatch(BaseModel):
    post_to_discord: Optional[bool] = None
    post_status:     Optional[str]  = None


# ── Admin UI ──────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def admin_home():
    with open("/app/static/admin.html") as f:
        return HTMLResponse(f.read())


# ── System status ─────────────────────────────────────────────

@api_router.get("/api/status")
async def status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SchedulerState))
    states = {s.job_name: s for s in result.scalars().all()}

    cfg     = await db.execute(select(DiscordConfig))
    discord_cfg = cfg.scalar_one_or_none()
    discord_ok, discord_bot = False, None
    if discord_cfg:
        discord_ok, discord_bot = await verify_token(discord_cfg.bot_token)

    regen = states.get("regenerate_occurrences")
    notif = states.get("pre_event_notifier")

    return {
        "service":          "samaya",
        "database":         "ok",
        "discord_connected": discord_ok,
        "discord_bot":       discord_bot,
        "scheduler": {
            "regenerate_occurrences": {
                "last_run":    regen.last_run_utc.isoformat() if regen and regen.last_run_utc else None,
                "last_result": regen.last_result if regen else None,
                "last_detail": regen.last_detail if regen else None,
            },
            "pre_event_notifier": {
                "last_run":    notif.last_run_utc.isoformat() if notif and notif.last_run_utc else None,
                "last_result": notif.last_result if notif else None,
            },
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Event definitions ─────────────────────────────────────────

@api_router.get("/api/events")
async def list_events(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(EventDefinition).order_by(EventDefinition.category, EventDefinition.name)
    )
    return [_event_dict(e) for e in result.scalars().all()]


@api_router.post("/api/events", status_code=201)
async def create_event(payload: EventIn, db: AsyncSession = Depends(get_db)):
    from datetime import time as dtime
    try:
        h, m = map(int, payload.start_time_utc.split(":"))
        t = dtime(h, m)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid start time: {e}")
    event = EventDefinition(
        name                    = payload.name,
        interval_days           = payload.interval_days,
        start_time_utc          = t,
        duration_hours          = payload.duration_hours,
        discord_channel         = payload.discord_channel,
        description             = payload.description,
        category                = payload.category,
        alliance                = payload.alliance,
        leadership_only         = payload.leadership_only,
        anchor_date             = date.fromisoformat(str(payload.anchor_date)),
        active                  = True,
        notification_channel_id = payload.notification_channel_id,
        notification_role_id    = payload.notification_role_id,
        notify_minutes_before   = payload.notify_minutes_before,
    )
    db.add(event)
    try:
        await db.commit()
        await db.refresh(event)
    except Exception as e:
        await db.rollback()
        err = str(e)
        if "ck_duration_positive" in err:
            raise HTTPException(status_code=422, detail="Duration must be greater than 0")
        if "ck_interval_positive" in err:
            raise HTTPException(status_code=422, detail="Interval must be at least 1 day")
        if "isoformat" in err or "anchor" in err.lower():
            raise HTTPException(status_code=422, detail="Anchor date must be in yyyy-mm-dd format")
        raise HTTPException(status_code=422, detail=f"Validation error: {err}")
    return _event_dict(event)


@api_router.patch("/api/events/{event_id}")
async def update_event(event_id: int, payload: EventPatch, db: AsyncSession = Depends(get_db)):
    from datetime import time as dtime
    result = await db.execute(select(EventDefinition).where(EventDefinition.id == event_id))
    event  = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if payload.name is not None:                    event.name                    = payload.name
    if payload.interval_days is not None:           event.interval_days           = payload.interval_days
    if payload.duration_hours is not None:          event.duration_hours          = payload.duration_hours
    if payload.discord_channel is not None:         event.discord_channel         = payload.discord_channel
    if payload.description is not None:             event.description             = payload.description
    if payload.category is not None:                event.category                = payload.category
    if payload.alliance is not None:                event.alliance                = payload.alliance
    if payload.leadership_only is not None:         event.leadership_only         = payload.leadership_only
    if payload.active is not None:                  event.active                  = payload.active
    if payload.anchor_date is not None:             event.anchor_date             = date.fromisoformat(payload.anchor_date)
    if payload.notification_channel_id is not None: event.notification_channel_id = payload.notification_channel_id
    if payload.notification_role_id is not None:    event.notification_role_id    = payload.notification_role_id
    if payload.notify_minutes_before is not None:   event.notify_minutes_before   = payload.notify_minutes_before
    if payload.start_time_utc is not None:
        try:
            h, m = map(int, payload.start_time_utc.split(":"))
            event.start_time_utc = dtime(h, m)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"Invalid start time: {e}")

    await db.commit()
    await db.refresh(event)
    return _event_dict(event)


@api_router.delete("/api/events/{event_id}", status_code=204)
async def deactivate_event(event_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(EventDefinition).where(EventDefinition.id == event_id))
    event  = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.active = False
    await db.commit()


# ── Preview ───────────────────────────────────────────────────


@api_router.delete("/api/events/{event_id}/permanent", status_code=200)
async def permanent_delete_event(event_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(EventDefinition).where(EventDefinition.id == event_id))
    event  = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Count PostLog entries for this event
    log_result = await db.execute(
        select(PostLog).where(PostLog.event_id == event_id)
    )
    log_entries = log_result.scalars().all()

    # Null out event_id on PostLog entries before deleting
    # so audit history is preserved with event_name still readable
    for log in log_entries:
        log.event_id = None

    # Delete the event — cascades to occurrences
    await db.delete(event)
    await db.commit()

    return {
        "status": "deleted",
        "event_name": event.name,
        "post_log_entries_preserved": len(log_entries),
    }

@api_router.post("/api/scheduler/preview")
async def preview_occurrences(payload: EventIn):
    anchor = date.fromisoformat(payload.anchor_date)
    dates  = next_occurrences(anchor, payload.interval_days, date.today(), count=10)
    return [{"date": str(d), "day": d.strftime("%A")} for d in dates]


# ── Occurrences ───────────────────────────────────────────────

@api_router.get("/api/occurrences")
async def list_occurrences(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Occurrence, EventDefinition)
        .join(EventDefinition)
        .order_by(Occurrence.occurrence_date, EventDefinition.name)
    )
    return [_occurrence_dict(occ, ev) for occ, ev in result.all()]


@api_router.patch("/api/occurrences/{occ_id}")
async def update_occurrence(occ_id: int, payload: OccurrencePatch, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Occurrence).where(Occurrence.id == occ_id))
    occ    = result.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    if payload.post_to_discord is not None: occ.post_to_discord = payload.post_to_discord
    if payload.post_status is not None:     occ.post_status     = payload.post_status
    await db.commit()
    return {"id": occ.id, "post_to_discord": occ.post_to_discord, "post_status": occ.post_status}


@api_router.post("/api/occurrences/{occ_id}/post")
async def post_occurrence(occ_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Occurrence, EventDefinition).join(EventDefinition).where(Occurrence.id == occ_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    occ, event = row

    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")

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

    existing = await db.execute(
        select(PostLog).where(
            PostLog.event_name == event.name,
            PostLog.occurrence_date == occ.occurrence_date,
        )
    )
    if existing.scalar_one_or_none():
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


@api_router.delete("/api/occurrences/{occ_id}/discord", status_code=200)
async def cancel_occurrence_discord(occ_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Occurrence, EventDefinition).join(EventDefinition).where(Occurrence.id == occ_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    occ, event = row

    log_result = await db.execute(
        select(PostLog).where(
            PostLog.event_name == event.name,
            PostLog.occurrence_date == occ.occurrence_date,
        )
    )
    log = log_result.scalar_one_or_none()
    if not log or not log.discord_event_id:
        raise HTTPException(status_code=404, detail="No Discord event ID found in PostLog")

    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")

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


# ── Manual regenerate ─────────────────────────────────────────

@api_router.post("/api/scheduler/regenerate")
async def manual_regenerate():
    await regenerate_occurrences()
    return {"status": "ok", "message": "Regeneration complete"}


# ── PostLog ───────────────────────────────────────────────────

@api_router.get("/api/post-log")
async def get_post_log(limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PostLog, EventDefinition)
        .outerjoin(EventDefinition, PostLog.event_id == EventDefinition.id)
        .order_by(PostLog.occurrence_date.desc(), PostLog.posted_at_utc.desc())
        .limit(limit).offset(offset)
    )
    return [_log_dict(l, ev) for l, ev in result.all()]


@api_router.get("/api/post-log/export.csv")
async def export_post_log(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PostLog).order_by(PostLog.occurrence_date.desc()))
    logs   = result.scalars().all()
    def generate():
        yield "Event Name,Date,Discord Event ID,Posted At (UTC),Posted By,Status\n"
        for l in logs:
            yield f'"{l.event_name}","{l.occurrence_date}","{l.discord_event_id or ""}","{l.posted_at_utc or ""}","{l.posted_by}","{l.status}"\n'
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=PostLog_Export.csv"}
    )


# ── Discord config ────────────────────────────────────────────

@api_router.put("/api/config/discord")
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



# ── Discord metadata (channels/roles) ──────────────────────────

@api_router.get("/api/discord/channels")
async def list_discord_channels(db: AsyncSession = Depends(get_db)):
    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")
    channels, error = await get_guild_channels(cfg.bot_token, cfg.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return channels


@api_router.get("/api/discord/roles")
async def list_discord_roles(db: AsyncSession = Depends(get_db)):
    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")
    roles, error = await get_guild_roles(cfg.bot_token, cfg.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return roles


# ── Discord Sync ──────────────────────────────────────────────

@api_router.get("/api/sync/discord")
async def sync_discord(db: AsyncSession = Depends(get_db)):
    """
    Fetches all current Discord scheduled events and compares
    against PostLog. Returns four categories:
    - matched: in both, details align
    - mismatched: in both, but fields differ from event definition
    - discord_only: on Discord but not in PostLog
    - postlog_only: in PostLog as posted but not found on Discord
    """
    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")

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


@api_router.post("/api/sync/push/{occ_id}")
async def sync_push_to_discord(occ_id: int, db: AsyncSession = Depends(get_db)):
    """
    Updates a Discord event to match the current event definition.
    Uses PATCH /guilds/{guild_id}/scheduled-events/{event_id}.
    """
    result = await db.execute(
        select(Occurrence, EventDefinition)
        .join(EventDefinition)
        .where(Occurrence.id == occ_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    occ, event = row

    log_result = await db.execute(
        select(PostLog).where(
            PostLog.event_name == event.name,
            PostLog.occurrence_date == occ.occurrence_date,
        )
    )
    log = log_result.scalar_one_or_none()
    if not log or not log.discord_event_id:
        raise HTTPException(status_code=404, detail="No Discord event ID in PostLog")

    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")

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


@api_router.post("/api/sync/acknowledge/{discord_event_id}")
async def acknowledge_discord_event(discord_event_id: str, db: AsyncSession = Depends(get_db)):
    """
    Adds a Discord-only event to PostLog as a manually-created record.
    """
    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")

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


@api_router.post("/api/sync/push-by-log/{post_log_id}")
async def sync_push_by_log(post_log_id: int, db: AsyncSession = Depends(get_db)):
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

    cfg_result = await db.execute(select(DiscordConfig))
    cfg = cfg_result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Discord not configured")

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


@api_router.post("/api/sync/mark-cancelled/{post_log_id}")
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

# ── Helpers ───────────────────────────────────────────────────

def _event_dict(e: EventDefinition) -> dict:
    return {
        "id":                       e.id,
        "name":                     e.name,
        "interval_days":            e.interval_days,
        "start_time_utc":           e.start_time_utc.strftime("%H:%M"),
        "duration_hours":           float(e.duration_hours),
        "discord_channel":          e.discord_channel,
        "description":              e.description,
        "category":                 e.category,
        "alliance":                 e.alliance,
        "leadership_only":          e.leadership_only,
        "active":                   e.active,
        "anchor_date":              str(e.anchor_date),
        "notification_channel_id":  e.notification_channel_id,
        "notification_role_id":     e.notification_role_id,
        "notify_minutes_before":    e.notify_minutes_before,
    }

def _occurrence_dict(occ: Occurrence, ev: EventDefinition) -> dict:
    return {
        "id":                 occ.id,
        "event_id":           occ.event_id,
        "event_name":         ev.name,
        "category":           ev.category,
        "occurrence_date":    str(occ.occurrence_date),
        "start_datetime_utc": occ.start_datetime_utc.isoformat(),
        "end_datetime_utc":   occ.end_datetime_utc.isoformat(),
        "post_to_discord":    occ.post_to_discord,
        "post_status":        occ.post_status,
        "status_detail":      occ.status_detail,
        "discord_channel":    ev.discord_channel,
        "duration_hours":     float(ev.duration_hours),
        "reminder_sent":      occ.reminder_sent,
        "alliance":           ev.alliance,
        "leadership_only":    ev.leadership_only,
    }

def _log_dict(l: PostLog, ev: EventDefinition | None = None) -> dict:
    today = date.today()
    diff  = (l.occurrence_date - today).days
    timing = "Today" if diff == 0 else (f"In {diff} days" if diff > 0 else f"{abs(diff)} days ago")
    return {
        "id":               l.id,
        "event_name":       l.event_name,
        "occurrence_date":  str(l.occurrence_date),
        "discord_event_id": l.discord_event_id,
        "posted_at_utc":    l.posted_at_utc.isoformat() if l.posted_at_utc else None,
        "posted_by":        l.posted_by,
        "status":           l.status,
        "status_detail":    l.status_detail,
        "timing":           timing,
        "alliance":         ev.alliance if ev else None,
        "leadership_only":  ev.leadership_only if ev else False,
    }

router.include_router(api_router)
