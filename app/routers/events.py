from datetime import date, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, Occurrence

router = APIRouter()

WINDOW_DAYS = 28


@router.get("/api/events")
async def list_events(db: AsyncSession = Depends(get_db)):
    today = date.today()
    end   = today + timedelta(days=WINDOW_DAYS)

    result = await db.execute(
        select(Occurrence, EventDefinition)
        .join(EventDefinition)
        .where(
            Occurrence.occurrence_date >= today,
            Occurrence.occurrence_date <= end,
            EventDefinition.active == True,
            EventDefinition.leadership_only == False,
        )
        .order_by(Occurrence.occurrence_date, EventDefinition.name)
    )
    rows = result.all()

    return JSONResponse([
        {
            "id":                 occ.id,
            "event_name":         event.name,
            "category":           event.category,
            "alliance":           event.alliance,
            "occurrence_date":    str(occ.occurrence_date),
            "start_datetime_utc": occ.start_datetime_utc.isoformat(),
            "end_datetime_utc":   occ.end_datetime_utc.isoformat(),
            "discord_channel":    event.discord_channel,
            "description":        event.description,
            "post_status":        occ.post_status,
            "duration_hours":     float(event.duration_hours),
        }
        for occ, event in rows
    ])


@router.get("/events", response_class=HTMLResponse)
async def events_page():
    with open("/app/static/events.html") as f:
        return HTMLResponse(f.read())
