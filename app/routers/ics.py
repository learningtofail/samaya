import hashlib
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from icalendar import Calendar, Event as ICalEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, Occurrence

router = APIRouter()

WINDOW_DAYS  = 28
LOOKAHEAD    = 7   # extra days beyond the window for subscribers


@router.get("/ics/events.ics")
async def ics_feed(db: AsyncSession = Depends(get_db)):
    today = date.today()
    end   = today + timedelta(days=WINDOW_DAYS + LOOKAHEAD)

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

    cal = Calendar()
    cal.add("version",  "2.0")
    cal.add("prodid",   "-//Samaya//taraka.dev//EN")
    cal.add("calname",  "Kingshot Events — ks138")
    cal.add("x-wr-calname", "Kingshot Events — ks138")
    cal.add("refresh-interval;value=duration", "PT1H")
    cal.add("x-published-ttl", "PT1H")

    for occ, event in rows:
        vevent = ICalEvent()

        # Deterministic UID — stable across regenerations
        uid_base = f"{event.name.lower().replace(' ', '-')}-{occ.occurrence_date}"
        uid      = hashlib.md5(uid_base.encode()).hexdigest() + "@ks138.taraka.dev"

        vevent.add("uid",     uid)
        vevent.add("summary", event.name)
        vevent.add("dtstart", occ.start_datetime_utc)
        vevent.add("dtend",   occ.end_datetime_utc)
        vevent.add("dtstamp", datetime.now(timezone.utc))
        vevent.add("location", event.discord_channel or "Discord")
        vevent.add("description", event.description or "")
        vevent.add("categories", [event.category])

        # STATUS based on post_status
        if occ.post_status in ("posted", "active", "completed"):
            vevent.add("status", "CONFIRMED")
        elif occ.post_status == "cancelled":
            vevent.add("status", "CANCELLED")
        else:
            vevent.add("status", "TENTATIVE")

        cal.add_component(vevent)

    content = cal.to_ical()

    return Response(
        content     = content,
        media_type  = "text/calendar; charset=utf-8",
        headers     = {
            "Content-Disposition": "inline; filename=kingshot-events.ics",
            "Cache-Control":       "max-age=3600",
        }
    )
