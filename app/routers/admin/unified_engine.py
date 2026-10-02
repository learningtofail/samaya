"""Delivery-engine routes for the unified event model (spec §66.4, §66.4a):
per-occurrence edits, the "this and following" split, and the delivery log.

Mounted under /api, next to unified_events.py.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from models import get_db
from models.db import (
    Delivery, Event, EventAlliance, EventAudience, EventOccurrence, EventReminder, Tenant, User,
)
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.discord_client import get_discord
from services.event_engine import (
    DISCORD_EVENT_FLOOR, effective_end, event_overview, effective_start, refresh_posted_discord_events,
    remove_posted_discord_events, sync_event_occurrences, apply_event_change, cancel_pending_deliveries,
)
from services.recurrence import occurs_on
from services.time_utils import ensure_utc

from .deps import get_current_tenants, get_current_user, require_not_viewer
from .unified_events import (
    _event_audit_snapshot, _event_dict, _load_event, _require_write_access, apply_event_patch,
)
from .unified_schemas import EventPatch

router = APIRouter(prefix="/api")

MAX_MESSAGE_CHARS = 2000


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    return ensure_utc(dt).isoformat() if dt is not None else None


def _occurrence_dict(occ: EventOccurrence, event: Event, delivery_counts: dict[str, int] | None = None,
                     overview: dict | None = None) -> dict:
    end = effective_end(occ)
    return {
        **(overview or {}),
        "id":                   occ.id,
        "event_id":             occ.event_id,
        "event_name":           event.name,
        "type":                 {"id": event.event_type.id, "name": event.event_type.name,
                                 "color": event.event_type.color},
        "leadership_only":      event.leadership_only,
        "occurrence_date":      str(occ.occurrence_date),
        "status":               occ.status,
        "start_datetime_utc":   _iso(effective_start(occ)),
        "end_datetime_utc":     _iso(end),
        "is_moved":             occ.start_datetime_utc_override is not None,
        "message_override":     occ.message_override,
        "delivery_counts":      delivery_counts or {},
    }


def _delivery_dict(d: Delivery, occ: EventOccurrence, event: Event, tenant: Tenant,
                   members: list[tuple[Delivery, Tenant]] | None = None) -> dict:
    """One send in the delivery log. `members` are the deliveries merged into
    it (spec §67.3): other destinations that share the channel, or alliances
    that share a Scheduled Event."""
    members = members or []
    names = sorted({tenant.name, *(t.name for _, t in members)}, key=str.lower)
    return {
        "id":                 d.id,
        "occurrence_id":      d.occurrence_id,
        "event_id":           event.id,
        "event_name":         event.name,
        "occurrence_date":    str(occ.occurrence_date),
        "tenant_slug":        tenant.slug,
        "tenant_name":        tenant.name,
        "alliance_names":     names,
        "kind":               d.kind,
        "reminder_minutes":   d.reminder_minutes if d.kind == "reminder" else None,
        "due_at_utc":         _iso(d.due_at_utc),
        "status":             d.status,
        "detail":             d.detail,
        "discord_event_id":   d.discord_event_id,
        "posted_at_utc":      _iso(d.posted_at_utc),
        "audience_label":     d.destination.audience.label if d.destination is not None else None,
        "channel_id":         d.channel_id or None,
        "guild_id":           d.guild_id or None,
        "merged_into_id":     d.merged_into_id,
        "members": [
            {
                "id": m.id, "tenant_name": t.name, "status": m.status, "detail": m.detail,
                "audience_label": m.destination.audience.label if m.destination is not None else None,
                "channel_id": m.channel_id or None,
            }
            for m, t in members
        ],
    }


# --------------------------------------------------------------------------
# Occurrences
# --------------------------------------------------------------------------

class OccurrencePatch(BaseModel):
    """Cancel or restore one occurrence, move it, or give it its own message.
    `message_override` of "" clears the override; `start_datetime_utc` of null
    with `clear_move` true puts the occurrence back on its normal time."""
    cancelled:          Optional[bool] = None
    start_datetime_utc: Optional[datetime] = None
    clear_move:         bool = False
    message_override:   Optional[str] = Field(default=None, max_length=MAX_MESSAGE_CHARS)

    @field_validator("start_datetime_utc", mode="after")
    @classmethod
    def _to_utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else (v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc))


async def _load_occurrence(db: AsyncSession, occurrence_id: int) -> tuple[EventOccurrence, Event]:
    occ = (await db.execute(
        select(EventOccurrence).where(EventOccurrence.id == occurrence_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if occ is None:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    event = await _load_event(db, occ.event_id)
    return occ, event


@router.get("/occurrences")
async def list_occurrences(
    tenants: list[Tenant] = Depends(get_current_tenants),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
):
    """Occurrences of everything visible from the selected alliance(s), with a
    count of deliveries by status. Defaults to the next 28 days."""
    start = date_from or datetime.now(timezone.utc).date()
    end = date_to or start + timedelta(days=28)
    tenant_ids = [t.id for t in tenants]
    kingdom_ids = {t.kingdom_id for t in tenants}
    in_audience = select(EventAlliance.event_id).where(EventAlliance.tenant_id.in_(tenant_ids))
    rows = await db.execute(
        select(EventOccurrence, Event)
        .join(Event, Event.id == EventOccurrence.event_id)
        .join(Tenant, Tenant.id == Event.owning_tenant_id)
        .where(
            EventOccurrence.occurrence_date >= start, EventOccurrence.occurrence_date <= end,
            or_(
                Event.owning_tenant_id.in_(tenant_ids), Event.id.in_(in_audience),
                (Event.scope == "kingdom-wide") & (Tenant.kingdom_id.in_(kingdom_ids)),
            ),
        )
        .order_by(EventOccurrence.start_datetime_utc, EventOccurrence.id)
    )
    pairs = rows.unique().all()
    counts: dict[int, dict[str, int]] = {}
    if pairs:
        count_rows = await db.execute(
            select(Delivery.occurrence_id, Delivery.status, func.count())
            .where(Delivery.occurrence_id.in_([o.id for o, _ in pairs]))
            .group_by(Delivery.occurrence_id, Delivery.status)
        )
        for occ_id, status, n in count_rows.all():
            counts.setdefault(occ_id, {})[status] = n
    overviews: dict[int, dict] = {}
    for _, e in pairs:
        if e.id not in overviews:
            overviews[e.id] = await event_overview(db, await _load_event(db, e.id))
    return [_occurrence_dict(o, e, counts.get(o.id), overviews[e.id]) for o, e in pairs]


@router.patch("/occurrences/{occurrence_id}")
async def update_occurrence(
    occurrence_id: int, payload: OccurrencePatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
):
    """The "this occurrence only" scope of §66.4a."""
    occ, event = await _load_occurrence(db, occurrence_id)
    await _require_write_access(db, user, tenant, event)
    given = payload.model_fields_set
    before = {"status": occ.status, "start": _iso(effective_start(occ)), "message_override": occ.message_override}
    now = datetime.now(timezone.utc)
    discord_errors: list[str] = []

    if payload.cancelled is True and payload.start_datetime_utc is not None:
        raise HTTPException(status_code=422, detail="Cannot cancel and move an occurrence in the same request")
    if payload.start_datetime_utc is not None and occ.status == "cancelled" and payload.cancelled is not False:
        raise HTTPException(status_code=422, detail="Restore the occurrence before moving it")

    if "message_override" in given and payload.message_override is not None:
        occ.message_override = payload.message_override.strip() or None
    if payload.clear_move:
        occ.start_datetime_utc_override = None
    if payload.start_datetime_utc is not None:
        original = ensure_utc(occ.start_datetime_utc)
        occ.start_datetime_utc_override = None if payload.start_datetime_utc == original else payload.start_datetime_utc

    if payload.cancelled is True and occ.status != "cancelled":
        occ.status = "cancelled"
        posted = (await db.execute(select(Delivery).where(
            Delivery.occurrence_id == occ.id, Delivery.kind == "discord_event", Delivery.status == "posted",
        ))).scalars().all()
        await cancel_pending_deliveries(db, [occ.id], "Occurrence cancelled")
        discord_errors += await remove_posted_discord_events(db, discord, list(posted))
    else:
        if payload.cancelled is False and occ.status == "cancelled":
            occ.status = "scheduled"
        if occ.status == "scheduled":
            await db.flush()
            await sync_event_occurrences(db, event, now)
            discord_errors += await refresh_posted_discord_events(db, discord, event, [occ], now)

    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, {})
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="event_occurrences", row_id=occ.id,
        action="update", before=before,
        after={"status": occ.status, "start": _iso(effective_start(occ)), "message_override": occ.message_override},
    )
    await db.commit()
    occ, event = await _load_occurrence(db, occurrence_id)
    body = _occurrence_dict(occ, event)
    if discord_errors:
        body["discord_errors"] = discord_errors
    return body


# --------------------------------------------------------------------------
# "This and following" (spec §66.4a)
# --------------------------------------------------------------------------

class SplitIn(BaseModel):
    """`from_date` is the first occurrence the change applies to; `changes`
    is an ordinary event patch applied to the new part of the series."""
    from_date: date
    changes:   EventPatch = EventPatch()


@router.post("/events/{event_id}/split", status_code=201)
async def split_event(
    event_id: int, payload: SplitIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
):
    """Ends the original series the day before `from_date` and starts a new
    event on `from_date` carrying the changes. Both share a series_id. The
    original's later occurrences are removed, including their Discord events;
    the new event generates its own."""
    event = await _load_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_write_access(db, user, tenant, event)
    if event.recurrence_kind == "none":
        raise HTTPException(status_code=422, detail="A one-off event has no later occurrences to split")
    if payload.from_date <= event.anchor_date:
        raise HTTPException(status_code=422, detail="Split date must be after the first occurrence; edit all occurrences instead")
    if not occurs_on(event.recurrence_kind, event.anchor_date, event.interval_days, event.until_date, payload.from_date):
        raise HTTPException(status_code=422, detail="The event does not occur on the split date")

    owner = await db.get(Tenant, event.owning_tenant_id)
    before = _event_audit_snapshot(event)
    now = datetime.now(timezone.utc)

    new_event = Event(
        owning_tenant_id=event.owning_tenant_id, type_id=event.type_id, series_id=event.series_id,
        name=event.name, scope=event.scope, leadership_only=event.leadership_only, message=event.message,
        location=event.location, start_time_utc=event.start_time_utc, duration_hours=event.duration_hours,
        recurrence_kind=event.recurrence_kind, interval_days=event.interval_days,
        anchor_date=payload.from_date, until_date=event.until_date, mention_role=event.mention_role,
        active=event.active, cover_image_data=event.cover_image_data,
    )
    new_event.reminders = [EventReminder(minutes_before=r.minutes_before) for r in event.reminders]
    new_event.alliances = [
        EventAlliance(tenant_id=a.tenant_id, message_override=a.message_override) for a in event.alliances
    ]
    new_event.audience_links = [
        EventAudience(audience_id=a.audience_id, included=a.included) for a in event.audience_links
    ]
    event.until_date = payload.from_date - timedelta(days=1)
    db.add(new_event)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, {})

    changes = payload.changes
    await apply_event_patch(db, user, new_event, owner, changes, new_event.scope)
    if changes.anchor_date is None:
        new_event.anchor_date = payload.from_date
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, {})

    discord_errors = await apply_event_change(db, discord, event, now)
    await sync_event_occurrences(db, new_event, now)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="events", row_id=event.id,
        action="update", before=before, after=_event_audit_snapshot(event),
    )
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="events", row_id=new_event.id,
        action="create", after=_event_audit_snapshot(new_event),
    )
    await db.commit()
    body = {
        "original": _event_dict(await _load_event(db, event.id)),
        "created":  _event_dict(await _load_event(db, new_event.id)),
    }
    if discord_errors:
        body["discord_errors"] = discord_errors
    return body


# --------------------------------------------------------------------------
# Delivery log, retry, health
# --------------------------------------------------------------------------

@router.get("/deliveries")
async def list_deliveries(
    tenants: list[Tenant] = Depends(get_current_tenants),
    status: str | None = Query(default=None),
    event_id: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    section: str | None = Query(default=None, pattern="^(upcoming|past)$"),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """One entry per send (spec §67.6): deliveries merged into another appear
    under it as `members`. Without `section`: newest first, `days` bounds by
    due time. `section=upcoming`: pending and sending, soonest first, no
    bound on how far ahead. `section=past`: everything else, newest first,
    bounded by `days`."""
    now = datetime.now(timezone.utc)
    tenant_ids = [t.id for t in tenants]
    child = aliased(Delivery)
    in_scope_via_member = select(child.merged_into_id).where(
        child.merged_into_id.is_not(None), child.tenant_id.in_(tenant_ids))
    stmt = (
        select(Delivery, EventOccurrence, Event, Tenant)
        .join(EventOccurrence, EventOccurrence.id == Delivery.occurrence_id)
        .join(Event, Event.id == EventOccurrence.event_id)
        .join(Tenant, Tenant.id == Delivery.tenant_id)
        .where(
            Delivery.merged_into_id.is_(None),
            or_(Delivery.tenant_id.in_(tenant_ids), Delivery.id.in_(in_scope_via_member)),
        )
        .limit(limit)
    )
    open_states = ("pending", "sending")
    if section == "upcoming":
        stmt = stmt.where(Delivery.status.in_(open_states)).order_by(Delivery.due_at_utc.asc(), Delivery.id.asc())
    else:
        if section == "past":
            stmt = stmt.where(Delivery.status.not_in(open_states))
        stmt = stmt.where(Delivery.due_at_utc >= now - timedelta(days=days)).order_by(
            Delivery.due_at_utc.desc(), Delivery.id.desc())
    if status:
        stmt = stmt.where(Delivery.status == status)
    if event_id is not None:
        stmt = stmt.where(Event.id == event_id)
    if kind:
        stmt = stmt.where(Delivery.kind == kind)
    rows = (await db.execute(stmt)).unique().all()
    members: dict[int, list[tuple[Delivery, Tenant]]] = {}
    if rows:
        found = await db.execute(
            select(Delivery, Tenant).join(Tenant, Tenant.id == Delivery.tenant_id)
            .where(Delivery.merged_into_id.in_([d.id for d, _, _, _ in rows]))
            .order_by(Delivery.id)
        )
        for m, t in found.unique().all():
            members.setdefault(m.merged_into_id, []).append((m, t))
    return [_delivery_dict(d, o, e, t, members.get(d.id)) for d, o, e, t in rows]


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(
    delivery_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Puts an `error` delivery back to `pending`; the next tick sends it.
    Only errors can be retried, and only while the event has not started."""
    d = await db.get(Delivery, delivery_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    occ, event = await _load_occurrence(db, d.occurrence_id)
    await _require_write_access(db, user, tenant, event)
    if d.status != "error":
        raise HTTPException(status_code=409, detail="Only a delivery in error can be retried")
    if occ.status == "cancelled" or not event.active:
        raise HTTPException(status_code=409, detail="The occurrence is cancelled")
    if effective_start(occ) - datetime.now(timezone.utc) < (DISCORD_EVENT_FLOOR if d.kind == "discord_event" else timedelta(0)):
        raise HTTPException(status_code=409, detail="Too close to the start time to send this")
    before = {"status": d.status, "detail": d.detail}
    d.status, d.detail, d.claimed_at_utc = "pending", "Retry requested", None
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="deliveries", row_id=d.id,
        action="update", before=before, after={"status": "pending"},
    )
    await db.commit()
    tenant_row = await db.get(Tenant, d.tenant_id)
    return _delivery_dict(d, occ, event, tenant_row)


@router.get("/delivery-health")
async def delivery_health(
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    """Trailing 7 days of deliveries that have come due, by status, plus how
    overdue the oldest pending one is. A pending delivery older than a few
    minutes means the tick is not running."""
    now = datetime.now(timezone.utc)
    tenant_ids = [t.id for t in tenants]
    scope = (Delivery.tenant_id.in_(tenant_ids), Delivery.merged_into_id.is_(None), Delivery.due_at_utc <= now,
             Delivery.due_at_utc >= now - timedelta(days=7))
    rows = await db.execute(select(Delivery.status, func.count()).where(*scope).group_by(Delivery.status))
    counts = {status: n for status, n in rows.all()}
    oldest = (await db.execute(
        select(func.min(Delivery.due_at_utc)).where(Delivery.tenant_id.in_(tenant_ids), Delivery.status == "pending",
                                                    Delivery.due_at_utc <= now)
    )).scalar_one()
    overdue = round((now - ensure_utc(oldest)).total_seconds() / 60) if oldest is not None else 0
    return {
        "window_days": 7,
        "counts": counts,
        "oldest_pending_overdue_minutes": overdue,
        "healthy": counts.get("error", 0) == 0 and overdue <= 5,
    }
