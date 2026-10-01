import time
from datetime import date, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Delivery, Event, EventOccurrence, Kingdom, Tenant
from services.discord_api import get_guild_channels
from services.event_engine import effective_end, effective_start, platform_bot_token
from services.public_events import PublicRow, public_rows
from services.static_assets import bust_static_cache

router = APIRouter()

# Spec §53 — resolving a notification's destination channel *name* for
# public display. The public page has no bot-API access of its own (see
# _announcement_row_dict's older docstring on why a raw channel ID was
# never shown before this), so the server resolves it once, using
# whichever tenant's own bot token is already available from this
# request's DB session, and caches the {channel_id: name} map per guild
# for a few minutes — an unauthenticated page can get bursty traffic, and
# nothing about a guild's channel list changes often enough to justify a
# live Discord API call on every single page view.
_channel_name_cache: dict[int, tuple[dict[str, str], float]] = {}
_CHANNEL_CACHE_TTL_SECONDS = 300


async def _resolve_channel_name(server, channel_id: str) -> str | None:
    """`server` is the DiscordServer the destination lives on (spec §67)."""
    if not channel_id:
        return None
    token = server.bot_token or platform_bot_token()
    if not token:
        return None

    now = time.monotonic()
    cached = _channel_name_cache.get(server.id)
    if cached is not None and now - cached[1] <= _CHANNEL_CACHE_TTL_SECONDS:
        return cached[0].get(channel_id)

    channels, error = await get_guild_channels(token, server.guild_id)
    if error:
        # Serve a stale cache entry on a transient Discord/network failure
        # rather than silently blanking out a channel name that was
        # showing fine a moment ago.
        return cached[0].get(channel_id) if cached else None

    channel_map = {c["id"]: c["name"] for c in channels}
    _channel_name_cache[server.id] = (channel_map, now)
    return channel_map.get(channel_id)

_DEFAULT_PUBLIC_SITE_TITLE = "Kingshot Event Schedule"

WINDOW_DAYS = 28


async def _get_tenant_by_slug(tenant_slug: str, db: AsyncSession) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail=f"No such alliance: {tenant_slug}")
    return tenant


def _row_dict(row: PublicRow) -> dict:
    """The shape events-public.js reads (spec §62, §66.6). `kind` stays
    "event" for something with a calendar entry and "announcement" for a
    message with no duration, so the page's existing rendering paths still
    apply; `type` carries the event type's name and color."""
    event, occ = row.event, row.occurrence
    start, end = effective_start(occ), effective_end(occ)
    reminders = sorted((r.minutes_before for r in event.reminders), reverse=True)
    data = {
        "kind":                 "event" if row.has_calendar_entry else "announcement",
        "id":                   occ.id,
        "event_name":           event.name,
        "type":                 {"name": event.event_type.name, "color": event.event_type.color},
        "scope":                event.scope,
        "occurrence_date":      str(occ.occurrence_date),
        "start_datetime_utc":   start.isoformat(),
        "end_datetime_utc":     end.isoformat() if end else None,
        "discord_channel":      event.location,
        "description":          row.message,
        "post_status":          row.status,
        "duration_hours":       float(event.duration_hours) if event.duration_hours is not None else None,
        "notify_minutes_before": reminders[0] if reminders else None,
        "reminder_minutes":     reminders,
        "event_offset_minutes": None,
        "notification_channel_name": None,
        "cover_image_data":     event.cover_image_data,
    }
    if row.with_tenant_fields:
        data["tenant_name"] = row.tenant.name
        data["tenant_slug"] = row.tenant.slug
        data["tenant_color"] = row.tenant.color
    return data


async def _rows_to_json(rows: list[PublicRow]) -> list[dict]:
    out = []
    for row in rows:
        data = _row_dict(row)
        data["notification_channel_name"] = (
            await _resolve_channel_name(row.destination.server, row.channel_id) if row.destination else None
        )
        out.append(data)
    return out


def _window() -> tuple[date, date]:
    today = date.today()
    return today, today + timedelta(days=WINDOW_DAYS)


@router.get("/t/{tenant_slug}/api/events")
async def list_events(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    tenant = await _get_tenant_by_slug(tenant_slug, db)
    start, end = _window()
    return JSONResponse(await _rows_to_json(await public_rows(db, start, end, tenant)))


@router.get("/t/{tenant_slug}/events", response_class=HTMLResponse)
async def events_page(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    await _get_tenant_by_slug(tenant_slug, db)  # 404s early for an unknown slug
    with open("/app/static/events.html") as f:
        # Spec §62 — events.html now loads its own external events.css/
        # events-public.js under /static/, which Cloudflare edge-caches by
        # extension the same way it does admin.html's assets (spec §23);
        # this page never needed busting before that, since everything was
        # inline.
        return HTMLResponse(bust_static_cache(f.read()))


@router.get("/api/alliances")
async def list_alliances(db: AsyncSession = Depends(get_db)):
    """Public, unauthenticated roster of every alliance's public-page
    identity (name/slug/color) — powers the alliance switcher on the
    public events pages (spec §26). Not gated the way admin's
    GET /admin/api/tenants is: a Tenant's name/slug/color are already
    exposed via the combined /api/events payload (for the tenant badge)
    and via the /t/{slug}/events URL itself, so this adds no new
    exposure, just a way to list them without already knowing one."""
    result = await db.execute(select(Tenant).order_by(Tenant.name))
    return JSONResponse([
        {
            "name": t.name, "slug": t.slug, "color": t.color,
            "icon_image_data": t.icon_image_data or None,
        }
        for t in result.scalars().all()
    ])


@router.get("/api/kingdom-branding")
async def get_kingdom_branding(db: AsyncSession = Depends(get_db)):
    """Public, unauthenticated (spec §38.7) — the same trust level as
    GET /api/alliances above: a display title is not sensitive, and both
    the public events pages and the admin console masthead need this
    before a user has necessarily logged in. This deployment is a single
    Kingdom (ks138.taraka.dev), so "the Kingdom" is just the first row;
    a deployment with none yet (or neither title ever set) gets the
    hardcoded defaults, unchanged from before this field existed."""
    result = await db.execute(select(Kingdom).order_by(Kingdom.id).limit(1))
    kingdom = result.scalar_one_or_none()
    return JSONResponse({
        "public_site_title":   (kingdom.public_site_title if kingdom else None) or _DEFAULT_PUBLIC_SITE_TITLE,
        "admin_console_title": (kingdom.admin_console_title if kingdom else None) or "Samaya",
    })


@router.get("/api/events")
async def list_events_all(db: AsyncSession = Depends(get_db)):
    """The combined calendar: every alliance's public events on one page. A
    kingdom-wide event is one row; an alliance event is one row per alliance
    in its audience, which the page groups back into one card."""
    start, end = _window()
    return JSONResponse(await _rows_to_json(await public_rows(db, start, end)))


async def _last_activity_for_tenants(db: AsyncSession, tenant_ids: list[int]) -> dict | None:
    """The most recent delivery that actually went out for these alliances.
    Leadership-only events are skipped: their existence is not public."""
    if not tenant_ids:
        return None
    row = (await db.execute(
        select(Delivery, Event)
        .join(EventOccurrence, EventOccurrence.id == Delivery.occurrence_id)
        .join(Event, Event.id == EventOccurrence.event_id)
        .where(Delivery.tenant_id.in_(tenant_ids), Delivery.status == "posted",
               Delivery.posted_at_utc.is_not(None), Event.leadership_only.is_(False))
        .order_by(Delivery.posted_at_utc.desc(), Delivery.id.desc()).limit(1)
    )).first()
    if row is None:
        return None
    delivery, event = row
    return {
        "kind": "event" if event.duration_hours is not None else "announcement",
        "name": event.name,
        "at": delivery.posted_at_utc.replace(tzinfo=delivery.posted_at_utc.tzinfo or timezone.utc)
                                    .astimezone(timezone.utc).isoformat(),
        "tenant_id": delivery.tenant_id,
    }


@router.get("/t/{tenant_slug}/api/last-activity")
async def last_activity(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    tenant = await _get_tenant_by_slug(tenant_slug, db)
    activity = await _last_activity_for_tenants(db, [tenant.id])
    if not activity:
        return JSONResponse(None)
    activity.pop("tenant_id", None)
    return JSONResponse(activity)


@router.get("/api/last-activity")
async def last_activity_all(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tenant))
    tenants = result.scalars().unique().all()
    activity = await _last_activity_for_tenants(db, [t.id for t in tenants])
    if not activity:
        return JSONResponse(None)
    t = next((t for t in tenants if t.id == activity["tenant_id"]), None)
    activity["tenant_name"] = t.name if t else None
    activity["tenant_slug"] = t.slug if t else None
    activity.pop("tenant_id", None)
    return JSONResponse(activity)


@router.get("/events", response_class=HTMLResponse)
async def events_page_all():
    with open("/app/static/events.html") as f:
        return HTMLResponse(bust_static_cache(f.read()))
