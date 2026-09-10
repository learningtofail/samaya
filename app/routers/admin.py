from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import (
    DiscordConfig, EventDefinition, Occurrence,
    PostLog, SchedulerState
)
from services.discord_api import (
    cancel_discord_event, create_discord_event,
    send_channel_message, verify_token
)
from services.recurrence import next_occurrences, build_start_datetime
from scheduler.jobs import regenerate_occurrences

router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────

class EventIn(BaseModel):
    name:                    str
    interval_days:           int
    start_time_utc:          str
    duration_hours:          float
    discord_channel:         str = ""
    description:             str = ""
    category:                str = "Other"
    anchor_date:             str
    notification_channel_id: str = ""
    notification_role_id:    str = ""
    notify_minutes_before:   Optional[int] = None

    @field_validator("interval_days", mode="before")
    @classmethod
    def validate_interval(cls, v):
        try:
            val = int(v)
        except (TypeError, ValueError):
            raise ValueError("Interval must be a whole number")
        if val < 1:
            raise ValueError("Interval must be at least 1 day (1=daily, 7=weekly, 14=biweekly, 28=every 4 weeks)")
        return val

    @field_validator("duration_hours", mode="before")
    @classmethod
    def validate_duration(cls, v):
        try:
            val = float(v)
        except (TypeError, ValueError):
            raise ValueError("Duration must be a number")
        if val <= 0:
            raise ValueError("Duration must be greater than 0 hours")
        return val

    @field_validator("start_time_utc", mode="before")
    @classmethod
    def validate_time(cls, v):
        import re
        v = str(v).strip()
        if not re.match(r"^\d{1,2}:\d{2}$", v):
            raise ValueError("Start time must be in HH:MM format (e.g. 19:00)")
        h, m = map(int, v.split(":"))
        if not (0 <= h <= 23):
            raise ValueError(f"Hour {h} is invalid — must be 00 to 23")
        if not (0 <= m <= 59):
            raise ValueError(f"Minute {m} is invalid — must be 00 to 59")
        return f"{h:02d}:{m:02d}"

    @field_validator("anchor_date", mode="before")
    @classmethod
    def validate_anchor(cls, v):
        from datetime import date as d
        try:
            d.fromisoformat(str(v))
        except (ValueError, TypeError):
            raise ValueError("Anchor date must be in yyyy-mm-dd format (e.g. 2025-05-01)")
        return str(v)

    @field_validator("notify_minutes_before", mode="before")
    @classmethod
    def validate_notify_minutes(cls, v):
        if v is None or v == "" or v == "null":
            return None
        try:
            val = int(v)
        except (TypeError, ValueError):
            raise ValueError("Notify minutes must be a whole number")
        if val < 1:
            raise ValueError("Notify minutes must be at least 1")
        return val


class EventPatch(BaseModel):
    name:                    Optional[str]  = None
    interval_days:           Optional[int]  = None
    start_time_utc:          Optional[str]  = None
    duration_hours:          Optional[float]= None
    discord_channel:         Optional[str]  = None
    description:             Optional[str]  = None
    category:                Optional[str]  = None
    active:                  Optional[bool] = None
    anchor_date:             Optional[str]  = None
    notification_channel_id: Optional[str]  = None
    notification_role_id:    Optional[str]  = None
    notify_minutes_before:   Optional[int]  = None

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

@router.get("/api/status")
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

@router.get("/api/events")
async def list_events(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(EventDefinition).order_by(EventDefinition.category, EventDefinition.name)
    )
    return [_event_dict(e) for e in result.scalars().all()]


@router.post("/api/events", status_code=201)
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


@router.patch("/api/events/{event_id}")
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


@router.delete("/api/events/{event_id}", status_code=204)
async def deactivate_event(event_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(EventDefinition).where(EventDefinition.id == event_id))
    event  = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.active = False
    await db.commit()


# ── Preview ───────────────────────────────────────────────────

@router.post("/api/scheduler/preview")
async def preview_occurrences(payload: EventIn):
    anchor = date.fromisoformat(payload.anchor_date)
    dates  = next_occurrences(anchor, payload.interval_days, date.today(), count=10)
    return [{"date": str(d), "day": d.strftime("%A")} for d in dates]


# ── Occurrences ───────────────────────────────────────────────

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
    if (occ.start_datetime_utc - now).total_seconds() < 900:
        raise HTTPException(status_code=400, detail="Event starts in less than 15 minutes")

    existing = await db.execute(
        select(PostLog).where(
            PostLog.event_name == event.name,
            PostLog.occurrence_date == occ.occurrence_date,
        )
    )
    if existing.scalar_one_or_none():
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
        await db.commit()
        raise HTTPException(status_code=502, detail=error)

    # Write PostLog
    log = PostLog(
        event_id         = event.id,
        event_name       = event.name,
        occurrence_date  = occ.occurrence_date,
        discord_event_id = discord_id,
        discord_guild_id = cfg.guild_id,
        posted_at_utc    = datetime.now(timezone.utc),
        posted_by        = "coordinator",
        status           = "posted",
    )
    db.add(log)
    occ.post_status   = "posted"
    occ.status_detail = None

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

    old_id = log.discord_event_id
    log.discord_event_id = f"CANCELLED — was {old_id}"
    log.status = "cancelled"
    occ.post_status = "cancelled"
    await db.commit()

    if not success and "404" not in error:
        raise HTTPException(status_code=502, detail=error)
    return {"status": "cancelled", "discord_event_id": old_id}


# ── Manual regenerate ─────────────────────────────────────────

@router.post("/api/scheduler/regenerate")
async def manual_regenerate():
    await regenerate_occurrences()
    return {"status": "ok", "message": "Regeneration complete"}


# ── PostLog ───────────────────────────────────────────────────

@router.get("/api/post-log")
async def get_post_log(limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PostLog)
        .order_by(PostLog.occurrence_date.desc(), PostLog.posted_at_utc.desc())
        .limit(limit).offset(offset)
    )
    return [_log_dict(l) for l in result.scalars().all()]


@router.get("/api/post-log/export.csv")
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
    }

def _log_dict(l: PostLog) -> dict:
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
    }
