import hashlib
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from icalendar import Calendar, Event as ICalEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Tenant
from services.public_events import PublicRow, effective_end, effective_start, public_rows

router = APIRouter()

WINDOW_DAYS  = 28
LOOKAHEAD    = 7   # extra days beyond the window for subscribers


def _build_calendar(rows, calname: str, uid_prefix: str = "") -> bytes:
    """Shared VEVENT-building logic for both the per-tenant and combined
    feeds below. `rows` are PublicRow objects with a calendar entry (a
    message with no duration is not a calendar event and is left out, spec
    §66.6), so one function covers both feeds.

    uid_prefix defaults to "" so the per-tenant feed's UIDs stay
    byte-identical to what was already live before the combined feed
    existed — that feed may already have real subscribers, and changing
    its UID format would make every calendar app treat every event as
    brand new, duplicating entries for anyone already subscribed. Only
    the new combined feed passes a non-empty prefix, since it has no
    prior subscribers to break.
    """
    cal = Calendar()
    cal.add("version",  "2.0")
    cal.add("prodid",   "-//Samaya//taraka.dev//EN")
    cal.add("x-wr-calname", calname)
    cal.add("refresh-interval;value=duration", "PT1H")
    cal.add("x-published-ttl", "PT1H")

    for row in rows:
        occ, event, tenant_slug = row.occurrence, row.event, row.tenant.slug
        vevent = ICalEvent()

        # Deterministic UID — stable across regenerations. Includes the
        # tenant slug: a kingdom-wide event has one Occurrence but a
        # separate PostLog/Discord event per tenant, and each tenant's own
        # feed needs its own UID for the same underlying occurrence rather
        # than colliding on one.
        uid_base = f"{uid_prefix}{tenant_slug}-{event.name.lower().replace(' ', '-')}-{occ.occurrence_date}"
        uid      = hashlib.md5(uid_base.encode()).hexdigest() + "@ks138.taraka.dev"

        vevent.add("uid",     uid)
        vevent.add("summary", event.name)
        vevent.add("dtstart", effective_start(occ))
        vevent.add("dtend",   effective_end(occ))
        vevent.add("dtstamp", datetime.now(timezone.utc))
        vevent.add("location", event.location or "Discord")
        vevent.add("description", row.message)

        if row.status == "posted":
            vevent.add("status", "CONFIRMED")
        elif row.status == "cancelled":
            vevent.add("status", "CANCELLED")
        else:
            vevent.add("status", "TENTATIVE")

        cal.add_component(vevent)

    return cal.to_ical()


def _calendar_rows(rows: list[PublicRow]) -> list[PublicRow]:
    return [r for r in rows if r.has_calendar_entry]


@router.api_route("/events/{tenant_slug}.ics", methods=["GET", "HEAD"])
async def ics_feed(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail=f"No such alliance: {tenant_slug}")

    today = date.today()
    rows = _calendar_rows(await public_rows(db, today, today + timedelta(days=WINDOW_DAYS + LOOKAHEAD), tenant))
    content = _build_calendar(rows, f"Kingshot Events — {tenant.name}")

    return Response(
        content     = content,
        media_type  = "text/calendar; charset=utf-8",
        headers     = {
            "Content-Disposition": f"inline; filename={tenant.slug}-kingshot-events.ics",
            "Cache-Control":       "max-age=3600",
        }
    )


@router.api_route("/events.ics", methods=["GET", "HEAD"])
async def ics_feed_all(db: AsyncSession = Depends(get_db)):
    """The combined feed: every alliance's public events in one calendar."""
    today = date.today()
    rows = _calendar_rows(await public_rows(db, today, today + timedelta(days=WINDOW_DAYS + LOOKAHEAD)))
    content = _build_calendar(rows, "Kingshot Events — All Alliances", uid_prefix="all-")

    return Response(
        content     = content,
        media_type  = "text/calendar; charset=utf-8",
        headers     = {
            "Content-Disposition": "inline; filename=kingshot-events-all.ics",
            "Cache-Control":       "max-age=3600",
        }
    )
