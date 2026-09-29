from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Announcement, AnnouncementTarget, EventDefinition, Kingdom, Occurrence, PostLog, Tenant

router = APIRouter()

_DEFAULT_PUBLIC_SITE_TITLE = "Kingshot Event Schedule"

WINDOW_DAYS = 28

# Scheduled/already-posted announcements only (spec §37) — 'draft' isn't a
# real public-facing state (nothing in the admin UI even creates one today;
# see models.db.Announcement's status CheckConstraint for the full set),
# and 'cancelled'/'failed' are deliberately left off the public page the
# same way a cancelled Occurrence still shows (it gets a "Cancelled" badge,
# §36's screenshot shows the pattern) — but a failed/cancelled announcement
# has no useful "when will this go out" information left to show, unlike an
# event, which keeps its own fixed schedule regardless of post outcome.
_PUBLIC_ANNOUNCEMENT_STATUSES = ("scheduled", "posted")


async def _get_tenant_by_slug(tenant_slug: str, db: AsyncSession) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail=f"No such alliance: {tenant_slug}")
    return tenant


def _event_row_dict(occ: Occurrence, event: EventDefinition) -> dict:
    return {
        "kind":                 "event",
        "id":                   occ.id,
        "event_name":           event.name,
        "scope":                event.scope,
        "occurrence_date":      str(occ.occurrence_date),
        "start_datetime_utc":   occ.start_datetime_utc.isoformat(),
        "end_datetime_utc":     occ.end_datetime_utc.isoformat(),
        "discord_channel":      event.discord_channel,
        "description":          event.description,
        "post_status":          occ.post_status,
        "duration_hours":       float(event.duration_hours),
        "notify_minutes_before": event.notify_minutes_before,
    }


def _announcement_row_dict(a: Announcement) -> dict:
    """Spec §37 — surfaces a scheduled/posted Announcement on the public
    events page, shaped as close to an event row as an Announcement's own
    fields allow so the client can group/sort/render both kinds with one
    code path (distinguished by "kind"). Deliberately NOT added to the ICS
    feed (routers/ics.py is untouched by this) — an Announcement has no
    duration or end time, so it isn't a calendar event, just a scheduled
    Discord text post the public page also wants to list.

    No discord_channel is included: unlike EventDefinition.discord_channel
    (a free-text display name), AnnouncementTarget only stores the raw
    numeric Discord channel ID, which isn't meaningful to show on an
    unauthenticated page with no bot-API access to resolve it to a name.
    """
    sched_utc = a.scheduled_for.astimezone(timezone.utc)
    return {
        "kind":                 "announcement",
        "id":                   a.id,
        "event_name":           a.title,
        "scope":                None,
        "occurrence_date":      str(sched_utc.date()),
        "start_datetime_utc":   sched_utc.isoformat(),
        "end_datetime_utc":     None,
        "discord_channel":      None,
        "description":          a.body_markdown,
        "post_status":          a.status,
        "duration_hours":       None,
        "notify_minutes_before": None,
        # How far ahead of the event it references this announcement goes
        # out (spec §27's "warn ahead of an event" pattern) — the
        # announcement equivalent of an event's notify_minutes_before,
        # shown the same way on the public page. 0 (the default, meaning
        # "no referenced event") renders no badge at all, same as an event
        # with notify_minutes_before unset.
        "event_offset_minutes": a.event_offset_minutes,
    }


def _window_bounds() -> tuple[date, date, datetime, datetime]:
    today = date.today()
    end   = today + timedelta(days=WINDOW_DAYS)
    ann_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    ann_end   = datetime.combine(end, time.max, tzinfo=timezone.utc)
    return today, end, ann_start, ann_end


def _sort_key(row: dict):
    return (row["occurrence_date"], row["start_datetime_utc"])


@router.get("/t/{tenant_slug}/api/events")
async def list_events(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    tenant = await _get_tenant_by_slug(tenant_slug, db)
    today, end, ann_start, ann_end = _window_bounds()

    # This tenant's own events, plus kingdom-wide events owned by any
    # tenant sharing this one's Kingdom (see models.db.EventDefinition.scope).
    result = await db.execute(
        select(Occurrence, EventDefinition)
        .join(EventDefinition)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .where(
            Occurrence.occurrence_date >= today,
            Occurrence.occurrence_date <= end,
            EventDefinition.active == True,
            EventDefinition.leadership_only == False,
            or_(
                EventDefinition.owning_tenant_id == tenant.id,
                (EventDefinition.scope == "kingdom-wide") & (Tenant.kingdom_id == tenant.kingdom_id),
            ),
        )
        .order_by(Occurrence.occurrence_date, EventDefinition.name)
    )
    rows = [_event_row_dict(occ, event) for occ, event in result.all()]

    # This tenant's own announcement targets (spec §37) — each
    # AnnouncementTarget names exactly one tenant, so "targets this
    # tenant" is the whole scoping rule; no kingdom-wide equivalent exists
    # for announcements the way it does for events.
    ann_result = await db.execute(
        select(Announcement)
        .join(AnnouncementTarget, AnnouncementTarget.announcement_id == Announcement.id)
        .where(
            AnnouncementTarget.tenant_id == tenant.id,
            Announcement.leadership_only == False,
            Announcement.status.in_(_PUBLIC_ANNOUNCEMENT_STATUSES),
            Announcement.scheduled_for >= ann_start,
            Announcement.scheduled_for <= ann_end,
        )
        .order_by(Announcement.scheduled_for)
    )
    rows += [_announcement_row_dict(a) for a in ann_result.scalars().all()]
    rows.sort(key=_sort_key)

    return JSONResponse(rows)


@router.get("/t/{tenant_slug}/events", response_class=HTMLResponse)
async def events_page(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    await _get_tenant_by_slug(tenant_slug, db)  # 404s early for an unknown slug
    with open("/app/static/events.html") as f:
        return HTMLResponse(f.read())


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
    """The bare, combined calendar — every tenant's events on one page,
    the pre-multi-tenant /events behavior, kept alongside (not instead
    of) the per-alliance /t/{slug}/events pages since some members want
    one shared view rather than switching between alliance pages.

    Scoped to every tenant in this deployment's database, not "every
    tenant in every Kingdom that might ever exist here" — this instance
    is specifically ks138.taraka.dev, one Kingdom's deployment. A second,
    unrelated Kingdom sharing this same database would need this route
    revisited rather than assumed to still mean "everyone."
    """
    today, end, ann_start, ann_end = _window_bounds()

    result = await db.execute(
        select(Occurrence, EventDefinition, Tenant)
        .join(EventDefinition, Occurrence.event_id == EventDefinition.id)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .where(
            Occurrence.occurrence_date >= today,
            Occurrence.occurrence_date <= end,
            EventDefinition.active == True,
            EventDefinition.leadership_only == False,
        )
        .order_by(Occurrence.occurrence_date, EventDefinition.name)
    )
    rows = []
    for occ, event, tenant in result.all():
        row = _event_row_dict(occ, event)
        row["tenant_name"]  = tenant.name
        row["tenant_slug"]  = tenant.slug
        row["tenant_color"] = tenant.color
        rows.append(row)

    # Every tenant's announcement targets (spec §37) — one row per
    # (announcement, target tenant), same fan-out shape PostLog already
    # uses for events: an announcement sent to three alliances' Discord
    # servers is genuinely three independent posts, so it shows once per
    # target tenant here, each with that tenant's own badge — mirroring
    # how a kingdom-wide *event* instead shows once under its single
    # owning tenant, since that's a single Occurrence, not a fan-out.
    ann_result = await db.execute(
        select(Announcement, Tenant)
        .join(AnnouncementTarget, AnnouncementTarget.announcement_id == Announcement.id)
        .join(Tenant, AnnouncementTarget.tenant_id == Tenant.id)
        .where(
            Announcement.leadership_only == False,
            Announcement.status.in_(_PUBLIC_ANNOUNCEMENT_STATUSES),
            Announcement.scheduled_for >= ann_start,
            Announcement.scheduled_for <= ann_end,
        )
        .order_by(Announcement.scheduled_for)
    )
    for a, tenant in ann_result.all():
        row = _announcement_row_dict(a)
        row["tenant_name"]  = tenant.name
        row["tenant_slug"]  = tenant.slug
        row["tenant_color"] = tenant.color
        rows.append(row)

    rows.sort(key=_sort_key)
    return JSONResponse(rows)


async def _last_activity_for_tenants(db: AsyncSession, tenant_ids: list[int]) -> dict | None:
    """Spec §38.9 — whichever is more recent between a `posted`-or-
    `completed` PostLog row (an event's Discord post — `completed` included
    alongside `posted` so a finished event that Sync has since closed out,
    per §44's "naturally completed" reconciliation, doesn't drop out of
    "last activity" just because it's no longer literally mid-lifecycle)
    and a `posted` AnnouncementTarget (an announcement's delivery to one
    specific alliance — deliberately the per-target status, not the parent
    Announcement's aggregate status: a multi-target announcement can
    succeed for one alliance and fail for another, and this must never
    claim a message landed somewhere it didn't)."""
    if not tenant_ids:
        return None

    candidates = []

    log_result = await db.execute(
        select(PostLog)
        .where(PostLog.tenant_id.in_(tenant_ids), PostLog.status.in_(("posted", "completed")))
        .order_by(PostLog.posted_at_utc.desc())
        .limit(1)
    )
    log = log_result.scalars().first()
    if log and log.posted_at_utc:
        candidates.append(("event", log.event_name, log.posted_at_utc, log.tenant_id))

    ann_result = await db.execute(
        select(Announcement, AnnouncementTarget.tenant_id)
        .join(AnnouncementTarget, AnnouncementTarget.announcement_id == Announcement.id)
        .where(AnnouncementTarget.tenant_id.in_(tenant_ids), AnnouncementTarget.post_status == "posted")
        .order_by(Announcement.posted_at.desc())
        .limit(1)
    )
    ann_row = ann_result.first()
    if ann_row and ann_row[0].posted_at:
        ann, ann_tenant_id = ann_row
        candidates.append(("announcement", ann.title, ann.posted_at, ann_tenant_id))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[2], reverse=True)
    kind, name, at, tenant_id = candidates[0]
    return {
        "kind": kind, "name": name,
        "at": at.astimezone(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
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
    tenants = result.scalars().all()
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
        return HTMLResponse(f.read())
