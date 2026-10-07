"""Admin API for the unified event model (spec §66): event types and events.

Event definitions: create, read, update, deactivate, delete. Occurrences,
the per-occurrence overrides, the "this and following" split and deliveries
are in unified_engine.py.
"""
from datetime import date, datetime, time as dtime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import get_db
from models.db import (
    Event, EventAlliance, EventAudience, EventReminder, EventType, SignupGroup, Tenant, User,
)
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.discord_client import get_discord
from services.destinations import alliance_overrides, plan_destinations
from services.event_engine import (
    apply_event_change, event_overview, remove_event_from_discord, sync_event_occurrences,
)
from services.images import EVENT_COVER, process_data_uri_async
from services.signup import drop_event_signup, signup_overview, type_signup_roles
from services.validators import check_recurrence_shape

from .deps import (
    check_kingdom_coordinator, get_current_tenant, get_current_tenants, get_current_user, require_not_viewer,
    resolve_target_tenants,
)
from .unified_schemas import (
    AudienceChangeIn, EventAllianceIn, EventIn, EventPatch, EventPreviewIn, EventTypeIn, EventTypePatch,
)

router = APIRouter(prefix="/api")

_EVENT_TYPE_NAME_TAKEN = "An event type with that name already exists"
_EVENT_TYPE_MESSAGES = {
    "uq_event_type_name": _EVENT_TYPE_NAME_TAKEN,
    # SQLite (tests) reports a composite UNIQUE as the first column only,
    # normalized by services/db_errors.constraint_name() to this shape.
    "event_types_kingdom_id_key": _EVENT_TYPE_NAME_TAKEN,
    "ck_event_type_duration_positive": "Default duration must be greater than 0",
    "ck_event_type_interval_positive": "Default interval must be at least 1 day",
}


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------

def _event_type_dict(t: EventType) -> dict:
    return {
        "id":                       t.id,
        "kingdom_id":               t.kingdom_id,
        "name":                     t.name,
        "color":                    t.color,
        "default_duration_hours":   float(t.default_duration_hours) if t.default_duration_hours is not None else None,
        "default_interval_days":    t.default_interval_days,
        "default_message":          t.default_message,
        "default_reminder_minutes": list(t.default_reminder_minutes or []),
        "default_mention_role":     t.default_mention_role,
        "sort_order":               t.sort_order,
    }


def _event_dict(e: Event) -> dict:
    """Needs e.alliances and e.reminders loaded (selectinload), since a
    lazy load would raise under async SQLAlchemy."""
    return {
        "id":                e.id,
        "owning_tenant_id":  e.owning_tenant_id,
        "series_id":         e.series_id,
        "type_id":           e.type_id,
        "type":              {"id": e.event_type.id, "name": e.event_type.name, "color": e.event_type.color},
        "name":              e.name,
        "scope":             e.scope,
        "leadership_only":   e.leadership_only,
        "message":           e.message,
        "location":          e.location,
        "start_time_utc":    e.start_time_utc.strftime("%H:%M"),
        "duration_hours":    float(e.duration_hours) if e.duration_hours is not None else None,
        "has_calendar_entry": e.duration_hours is not None,
        "recurrence_kind":   e.recurrence_kind,
        "interval_days":     e.interval_days,
        "anchor_date":       str(e.anchor_date),
        "until_date":        str(e.until_date) if e.until_date else None,
        "mention_role":      e.mention_role,
        "clean_up_reminders": e.clean_up_reminders,
        "signup_enabled":    e.signup_enabled,
        "signup_mention":    e.signup_mention,
        "rsvp_enabled":      e.rsvp_enabled,
        "signup_group_id":   e.signup_group_id,
        "active":            e.active,
        "cover_image_data":  e.cover_image_data or None,
        "reminder_minutes":  sorted((r.minutes_before for r in e.reminders), reverse=True),
        "reminder_messages": {str(r.minutes_before): r.message for r in e.reminders if r.message},
        "alliances": [
            {
                "tenant_id":        a.tenant_id,
                "message_override": a.message_override,
            }
            for a in sorted(e.alliances, key=lambda a: a.tenant_id)
        ],
        "audience_changes": [
            {"audience_id": a.audience_id, "included": a.included}
            for a in sorted(e.audience_links, key=lambda a: a.audience_id)
        ],
    }


async def _event_body(db: AsyncSession, e: Event) -> dict:
    """The event plus its live delivery overview (spec §67.6)."""
    body = _event_dict(e)
    body.update(await event_overview(db, e))
    body.update(await signup_overview(db, e, body.get("servers") or []))
    return body


def _event_audit_snapshot(e: Event) -> dict:
    return {
        "name": e.name, "type_id": e.type_id, "scope": e.scope, "leadership_only": e.leadership_only,
        "message": e.message, "location": e.location, "start_time_utc": e.start_time_utc.strftime("%H:%M"),
        "duration_hours": float(e.duration_hours) if e.duration_hours is not None else None,
        "recurrence_kind": e.recurrence_kind, "interval_days": e.interval_days,
        "anchor_date": str(e.anchor_date), "until_date": str(e.until_date) if e.until_date else None,
        "mention_role": e.mention_role, "clean_up_reminders": e.clean_up_reminders, "active": e.active,
        "signup_enabled": e.signup_enabled, "signup_mention": e.signup_mention, "rsvp_enabled": e.rsvp_enabled,
        "signup_group_id": e.signup_group_id,
        "reminder_minutes": sorted((r.minutes_before for r in e.reminders), reverse=True),
        "reminder_messages": {str(r.minutes_before): r.message for r in e.reminders if r.message},
        "alliance_tenant_ids": sorted(a.tenant_id for a in e.alliances),
        "audience_changes": sorted((a.audience_id, a.included) for a in e.audience_links),
    }


# --------------------------------------------------------------------------
# Event types
# --------------------------------------------------------------------------

async def _get_type_in_kingdom(db: AsyncSession, type_id: int, kingdom_id: int) -> EventType:
    etype = await db.get(EventType, type_id)
    if etype is None or etype.kingdom_id != kingdom_id:
        raise HTTPException(status_code=404, detail="Event type not found")
    return etype


@router.get("/event-types")
async def list_event_types(tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(EventType).where(EventType.kingdom_id == tenant.kingdom_id).order_by(EventType.sort_order, EventType.name)
    )
    out = []
    for t in result.scalars().all():
        body = _event_type_dict(t)
        body["signup_roles"] = await type_signup_roles(db, t.id)  # spec §87: the fallback Notify me roles
        out.append(body)
    return out


@router.post("/event-types", status_code=201)
async def create_event_type(
    payload: EventTypeIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id)
    etype = EventType(
        kingdom_id=tenant.kingdom_id,
        name=payload.name,
        color=payload.color,
        default_duration_hours=payload.default_duration_hours,
        default_interval_days=payload.default_interval_days,
        default_message=payload.default_message,
        default_reminder_minutes=payload.default_reminder_minutes,
        default_mention_role=payload.default_mention_role,
        sort_order=payload.sort_order,
    )
    db.add(etype)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _EVENT_TYPE_MESSAGES)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="event_types", row_id=etype.id,
        action="create", after=_event_type_dict(etype),
    )
    await db.commit()
    return _event_type_dict(etype)


@router.patch("/event-types/{type_id}")
async def update_event_type(
    type_id: int, payload: EventTypePatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id)
    etype = await _get_type_in_kingdom(db, type_id, tenant.kingdom_id)
    before = _event_type_dict(etype)
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        # default_duration_hours and default_interval_days may be cleared with
        # an explicit null; every other field treats null as "unchanged".
        if value is None and field not in ("default_duration_hours", "default_interval_days"):
            continue
        setattr(etype, field, value)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _EVENT_TYPE_MESSAGES)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="event_types", row_id=etype.id,
        action="update", before=before, after=_event_type_dict(etype),
    )
    await db.commit()
    return _event_type_dict(etype)


@router.delete("/event-types/{type_id}", status_code=204)
async def delete_event_type(
    type_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id)
    etype = await _get_type_in_kingdom(db, type_id, tenant.kingdom_id)
    in_use = (await db.execute(select(func.count()).select_from(Event).where(Event.type_id == type_id))).scalar_one()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"{in_use} event(s) still use this type. Move them to another type first.",
        )
    before = _event_type_dict(etype)
    await db.delete(etype)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="event_types", row_id=type_id,
        action="delete", before=before,
    )
    await db.commit()


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

_EVENT_OPTIONS = (
    selectinload(Event.alliances), selectinload(Event.reminders),
    selectinload(Event.audience_links),
)


async def _load_event(db: AsyncSession, event_id: int) -> Event | None:
    # populate_existing: this runs right after a flush/commit on the same
    # session, where a just-created Event is already in the identity map with
    # relationships (event_type) never loaded.
    result = await db.execute(
        select(Event).options(*_EVENT_OPTIONS).where(Event.id == event_id).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _resolve_alliance_rows(
    db: AsyncSession, user: User, owner: Tenant, scope: str, rows: list[EventAllianceIn],
) -> list[EventAlliance]:
    """Builds the audience rows for an event.

    An 'alliance' event always includes its owner, plus any other listed
    alliances the caller has access to. A 'kingdom-wide' event reaches every
    alliance in the Kingdom automatically, so rows only carry per-alliance
    overrides and need no access check beyond the kingdom-coordinator check
    the caller already passed. Every alliance must be in the owner's Kingdom."""
    slugs = [r.tenant_slug for r in rows]
    if len(set(slugs)) != len(slugs):
        raise HTTPException(status_code=422, detail="Each alliance can be listed only once")

    if scope == "alliance":
        tenants_by_slug = await resolve_target_tenants(db, user, slugs)
    elif slugs:
        found = await db.execute(select(Tenant).where(Tenant.slug.in_(slugs)))
        tenants_by_slug = {t.slug: t for t in found.scalars().all()}
        missing = set(slugs) - set(tenants_by_slug)
        if missing:
            raise HTTPException(status_code=404, detail=f"Unknown tenant slug(s): {sorted(missing)}")
    else:
        tenants_by_slug = {}

    for t in tenants_by_slug.values():
        if t.kingdom_id != owner.kingdom_id:
            raise HTTPException(status_code=422, detail=f"{t.name} is not in this Kingdom")

    built = [
        EventAlliance(
            tenant_id=tenants_by_slug[r.tenant_slug].id,
            message_override=r.message_override or None,
        )
        for r in rows
    ]
    if scope == "alliance" and owner.id not in {a.tenant_id for a in built}:
        built.append(EventAlliance(tenant_id=owner.id))
    return built


def _check_reminder_messages(minutes: list[int], messages: dict[int, str] | None) -> None:
    """A message belongs to a reminder that exists (spec §77)."""
    stray = sorted(set(messages or {}) - set(minutes), reverse=True)
    if stray:
        raise HTTPException(
            status_code=422,
            detail=f"There is no reminder at {', '.join(str(m) for m in stray)} minutes to carry that message")


def _reminder_rows(minutes: list[int], messages: dict[int, str] | None = None) -> list[EventReminder]:
    return [EventReminder(minutes_before=m, message=(messages or {}).get(m)) for m in minutes]


def _sync_reminders(event: Event, minutes: list[int], messages: dict[int, str] | None = None) -> None:
    """Edits an event's reminder list in place. Replacing the collection
    wholesale would insert the new rows before deleting the old ones in the
    same flush and trip the (event_id, minutes_before) unique constraint
    whenever a value is kept."""
    wanted = set(minutes)
    kept = {r.minutes_before for r in event.reminders}
    for row in [r for r in event.reminders if r.minutes_before not in wanted]:
        event.reminders.remove(row)
    for m in sorted(wanted - kept, reverse=True):
        event.reminders.append(EventReminder(minutes_before=m))
    if messages is not None:  # a full replacement: offsets not in the map go back to the event message
        for row in event.reminders:
            row.message = messages.get(row.minutes_before)


def _sync_alliances(event: Event, rows: list[EventAlliance]) -> None:
    """Same in-place edit as _sync_reminders, for the audience rows
    (unique on event_id + tenant_id): existing rows keep their identity and
    take the new override values."""
    by_tenant = {a.tenant_id: a for a in event.alliances}
    wanted = {r.tenant_id: r for r in rows}
    for row in [a for a in event.alliances if a.tenant_id not in wanted]:
        event.alliances.remove(row)
    for tenant_id, new in wanted.items():
        existing = by_tenant.get(tenant_id)
        if existing is None:
            event.alliances.append(new)
            continue
        existing.message_override = new.message_override


async def _audience_in_memory(db: AsyncSession, owner: Tenant, scope: str, alliance_ids: set[int]) -> list[Tenant]:
    """The audience of an event that may not be saved yet (same rule as
    services.event_engine.audience_tenants)."""
    if scope == "kingdom-wide":
        found = await db.execute(select(Tenant).where(Tenant.kingdom_id == owner.kingdom_id).order_by(Tenant.id))
    else:
        found = await db.execute(select(Tenant).where(Tenant.id.in_(alliance_ids or {owner.id})).order_by(Tenant.id))
    return list(found.scalars().all())


def _sync_selection(event: Event, changes: list[AudienceChangeIn] | None) -> None:
    """Edits the event's per-Audience changes in place."""
    if changes is None:
        return
    wanted = {c.audience_id: c.included for c in changes}
    for row in [a for a in event.audience_links if a.audience_id not in wanted]:
        event.audience_links.remove(row)
    by_audience = {a.audience_id: a for a in event.audience_links}
    for aid, included in wanted.items():
        if aid in by_audience:
            by_audience[aid].included = included
        else:
            event.audience_links.append(EventAudience(audience_id=aid, included=included))


async def _validate_destinations(db: AsyncSession, owner: Tenant, event: Event) -> None:
    """Spec §67.2 and §67.4: reject a selection the event cannot use, and a
    shared channel whose alliances would get different messages. Nothing is
    saved when this raises."""
    tenants = await _audience_in_memory(db, owner, event.scope, {a.tenant_id for a in event.alliances})
    plan = await plan_destinations(
        db, tenants=tenants, owner=owner,
        changes={a.audience_id: a.included for a in event.audience_links},
        leadership_only=event.leadership_only, base_message=event.message or "",
        overrides=alliance_overrides(event.alliances),
    )
    if plan.problems:
        raise HTTPException(status_code=422, detail="; ".join(plan.problems))
    if plan.conflicts:
        raise HTTPException(status_code=422, detail=" ".join(c.message() for c in plan.conflicts))


def _parse_time(value: str) -> dtime:
    h, m = map(int, value.split(":"))
    return dtime(h, m)


async def _require_write_access(db: AsyncSession, user: User, tenant: Tenant, event: Event):
    """The caller must be acting as the event's owning alliance, or as a
    kingdom coordinator on a kingdom-wide event owned inside the same
    Kingdom. Anything else is reported as not found."""
    if event.owning_tenant_id == tenant.id:
        if event.scope == "kingdom-wide":
            await check_kingdom_coordinator(db, user, tenant.kingdom_id)
        return
    owner = await db.get(Tenant, event.owning_tenant_id)
    if event.scope == "kingdom-wide" and owner is not None and owner.kingdom_id == tenant.kingdom_id:
        await check_kingdom_coordinator(db, user, tenant.kingdom_id)
        return
    raise HTTPException(status_code=404, detail="Event not found")


async def _get_group_in_kingdom(db: AsyncSession, group_id: int, kingdom_id: int) -> SignupGroup:
    group = await db.get(SignupGroup, group_id)
    if group is None or group.kingdom_id != kingdom_id:
        raise HTTPException(status_code=422, detail="That attendance group does not belong to this Kingdom")
    return group


def _settle_buttons_for_leadership(event: Event, signup_asked: bool | None, rsvp_asked: bool | None) -> None:
    """Leadership-only events never get buttons (spec §87.2 decision 4). Asking for one is a 422; otherwise
    both are quietly forced off."""
    if not event.leadership_only:
        return
    if signup_asked or rsvp_asked:
        raise HTTPException(status_code=422, detail="A leadership-only event cannot have Notify me or I'm in buttons")
    event.signup_enabled = False
    event.rsvp_enabled = False


async def apply_event_patch(
    db: AsyncSession, user: User, event: Event, owner: Tenant, payload: EventPatch, before_scope: str,
) -> None:
    """Applies a partial update to `event` in memory (no flush). Shared by
    the whole-event PATCH and the "this and following" split (§66.4a)."""
    given = payload.model_fields_set

    new_scope = payload.scope if payload.scope is not None else event.scope
    if new_scope == "kingdom-wide":
        await check_kingdom_coordinator(db, user, owner.kingdom_id)

    if payload.type_id is not None:
        event.type_id = (await _get_type_in_kingdom(db, payload.type_id, owner.kingdom_id)).id
    for field in ("name", "leadership_only", "active", "message", "location", "mention_role", "clean_up_reminders",
                  "signup_enabled", "signup_mention", "rsvp_enabled"):
        value = getattr(payload, field)
        if field in given and value is not None:
            setattr(event, field, value)
    if "signup_group_id" in given:
        event.signup_group_id = (await _get_group_in_kingdom(db, payload.signup_group_id, owner.kingdom_id)).id \
            if payload.signup_group_id is not None else None
    _settle_buttons_for_leadership(event, payload.signup_enabled, payload.rsvp_enabled)
    event.scope = new_scope
    if payload.start_time_utc is not None:
        event.start_time_utc = _parse_time(payload.start_time_utc)
    if payload.anchor_date is not None:
        event.anchor_date = date.fromisoformat(payload.anchor_date)
    if "until_date" in given:
        event.until_date = date.fromisoformat(payload.until_date) if payload.until_date else None
    if "duration_hours" in given:
        event.duration_hours = payload.duration_hours
    if "cover_image_data" in given and payload.cover_image_data is not None:
        event.cover_image_data = (
            await process_data_uri_async(payload.cover_image_data, EVENT_COVER, "Cover image")
            if payload.cover_image_data else None)

    if "recurrence_kind" in given and payload.recurrence_kind is not None:
        event.recurrence_kind = payload.recurrence_kind
        if payload.recurrence_kind == "none" and "interval_days" not in given:
            event.interval_days = None
    if "interval_days" in given:
        event.interval_days = payload.interval_days
        if payload.interval_days is not None and "recurrence_kind" not in given:
            event.recurrence_kind = "interval_days"
    try:
        check_recurrence_shape(event.recurrence_kind, event.interval_days, event.anchor_date, event.until_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if payload.reminder_minutes is not None or payload.reminder_messages is not None:
        minutes = (payload.reminder_minutes if payload.reminder_minutes is not None
                   else [r.minutes_before for r in event.reminders])
        _check_reminder_messages(minutes, payload.reminder_messages)
        _sync_reminders(event, minutes, payload.reminder_messages)
    if payload.alliances is not None:
        _sync_alliances(event, await _resolve_alliance_rows(db, user, owner, new_scope, payload.alliances))
    elif payload.scope is not None and payload.scope != before_scope and new_scope == "alliance":
        # Switching a kingdom-wide event back to a single alliance leaves
        # only the owner as its audience.
        _sync_alliances(event, [EventAlliance(tenant_id=owner.id)])
    _sync_selection(event, payload.audience_changes)
    await _validate_destinations(db, owner, event)


@router.get("/events")
async def list_events(
    tenants: list[Tenant] = Depends(get_current_tenants),
    type_id: int | None = Query(default=None),
    scope: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Everything visible from the selected alliance(s): events they own,
    events that list them in the audience, and kingdom-wide events in their
    Kingdom. X-Tenant-Slug '*' means every accessible alliance."""
    tenant_ids = [t.id for t in tenants]
    kingdom_ids = {t.kingdom_id for t in tenants}
    in_audience = select(EventAlliance.event_id).where(EventAlliance.tenant_id.in_(tenant_ids))
    stmt = (
        select(Event)
        .join(Tenant, Event.owning_tenant_id == Tenant.id)
        .options(*_EVENT_OPTIONS)
        .where(
            or_(
                Event.owning_tenant_id.in_(tenant_ids),
                Event.id.in_(in_audience),
                (Event.scope == "kingdom-wide") & (Tenant.kingdom_id.in_(kingdom_ids)),
            )
        )
        .order_by(Event.name, Event.id)
    )
    if type_id is not None:
        stmt = stmt.where(Event.type_id == type_id)
    if scope is not None:
        stmt = stmt.where(Event.scope == scope)
    if active is not None:
        stmt = stmt.where(Event.active == active)
    result = await db.execute(stmt)
    return [await _event_body(db, e) for e in result.scalars().unique().all()]


@router.post("/events/preview-destinations")
async def preview_destinations(
    payload: EventPreviewIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Where an event would post, without saving it (spec §67.6). The Events
    form calls this instead of repeating the rules in JavaScript."""
    if payload.scope == "kingdom-wide":
        await check_kingdom_coordinator(db, user, tenant.kingdom_id)
    rows = await _resolve_alliance_rows(db, user, tenant, payload.scope, payload.alliances)
    tenants = await _audience_in_memory(db, tenant, payload.scope, {r.tenant_id for r in rows})
    plan = await plan_destinations(
        db, tenants=tenants, owner=tenant,
        changes={c.audience_id: c.included for c in payload.audience_changes},
        leadership_only=payload.leadership_only, base_message=payload.message,
        overrides=alliance_overrides(rows),
    )
    send_of = {t.id: send for send in plan.sends for t in send.targets}
    return {
        "audiences": [
            {
                "id": a.id, "label": a.label, "kingdom_id": a.kingdom_id, "leadership_only": a.leadership_only,
                "destinations": [
                    {"server_name": d.server.name, "channel_id": d.channel_id, "role_id": d.role_id}
                    for d in a.destinations
                ],
                "alliances": sorted({t.tenant.name for t in plan.resolution.targets if t.audience.id == a.id}, key=str.lower),
                "shares_channel_with": sorted({
                    o.label for d in a.destinations if d.id in send_of
                    for o in send_of[d.id].targets if o.audience.id != a.id}),
            }
            for a in plan.resolution.audiences
        ],
        "destination_count": len(plan.resolution.destinations),
        "channel_count": len(plan.sends),
        "conflicts": [c.message() for c in plan.conflicts],
        "problems": plan.problems,
        "dropped": [a.label for a in plan.resolution.dropped],
    }


@router.get("/events/{event_id}")
async def get_event(
    event_id: int, tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db),
):
    event = await _load_event(db, event_id)
    owner = await db.get(Tenant, event.owning_tenant_id) if event else None
    visible = event is not None and (
        event.owning_tenant_id == tenant.id
        or any(a.tenant_id == tenant.id for a in event.alliances)
        or (event.scope == "kingdom-wide" and owner.kingdom_id == tenant.kingdom_id)
    )
    if not visible:
        raise HTTPException(status_code=404, detail="Event not found")
    return await _event_body(db, event)


@router.post("/events", status_code=201)
async def create_event(
    payload: EventIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.scope == "kingdom-wide":
        await check_kingdom_coordinator(db, user, tenant.kingdom_id)

    etype = await _get_type_in_kingdom(db, payload.type_id, tenant.kingdom_id)
    given = payload.model_fields_set
    cover_image_data = (
        await process_data_uri_async(payload.cover_image_data, EVENT_COVER, "Cover image")
        if payload.cover_image_data else None)

    duration = payload.duration_hours if "duration_hours" in given else (
        float(etype.default_duration_hours) if etype.default_duration_hours is not None else None
    )
    if "recurrence_kind" in given:
        kind = payload.recurrence_kind
        if "interval_days" in given:
            interval = payload.interval_days
        else:
            interval = etype.default_interval_days if kind == "interval_days" else None
    elif "interval_days" in given:
        interval = payload.interval_days
        kind = "interval_days" if interval is not None else "none"
    else:
        interval = etype.default_interval_days
        kind = "interval_days" if interval is not None else "none"
    until = date.fromisoformat(payload.until_date) if payload.until_date else None
    anchor = date.fromisoformat(payload.anchor_date)
    try:
        check_recurrence_shape(kind, interval, anchor, until)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    message = payload.message if payload.message is not None else etype.default_message
    mention_role = payload.mention_role if payload.mention_role is not None else etype.default_mention_role
    reminder_minutes = (
        payload.reminder_minutes if payload.reminder_minutes is not None
        else sorted(etype.default_reminder_minutes or [], reverse=True)
    )

    event = Event(
        owning_tenant_id=tenant.id,
        type_id=etype.id,
        name=payload.name,
        scope=payload.scope,
        leadership_only=payload.leadership_only,
        message=message,
        location=payload.location,
        start_time_utc=_parse_time(payload.start_time_utc),
        duration_hours=duration,
        recurrence_kind=kind,
        interval_days=interval,
        anchor_date=anchor,
        until_date=until,
        mention_role=mention_role,
        clean_up_reminders=bool(payload.clean_up_reminders),
        signup_enabled=True if payload.signup_enabled is None else payload.signup_enabled,
        signup_mention=bool(payload.signup_mention),
        rsvp_enabled=True if payload.rsvp_enabled is None else payload.rsvp_enabled,
        active=True,
        cover_image_data=cover_image_data,
    )
    if payload.signup_group_id is not None:
        event.signup_group_id = (await _get_group_in_kingdom(db, payload.signup_group_id, tenant.kingdom_id)).id
    _settle_buttons_for_leadership(event, payload.signup_enabled, payload.rsvp_enabled)
    event.alliances = await _resolve_alliance_rows(db, user, tenant, payload.scope, payload.alliances)
    _check_reminder_messages(reminder_minutes, payload.reminder_messages)
    event.reminders = _reminder_rows(reminder_minutes, payload.reminder_messages)
    event.audience_links = []
    _sync_selection(event, payload.audience_changes)
    await _validate_destinations(db, tenant, event)
    db.add(event)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, {})
    await sync_event_occurrences(db, event, datetime.now(timezone.utc))
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="events", row_id=event.id,
        action="create", after=_event_audit_snapshot(event),
    )
    await db.commit()
    return await _event_body(db, await _load_event(db, event.id))


@router.patch("/events/{event_id}")
async def update_event(
    event_id: int, payload: EventPatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
):
    event = await _load_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_write_access(db, user, tenant, event)
    owner = await db.get(Tenant, event.owning_tenant_id)
    before = _event_audit_snapshot(event)
    await apply_event_patch(db, user, event, owner, payload, before["scope"])
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, {})
    discord_errors = await apply_event_change(db, discord, event, datetime.now(timezone.utc))
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="events", row_id=event.id,
        action="update", before=before, after=_event_audit_snapshot(event),
    )
    await db.commit()
    body = await _event_body(db, await _load_event(db, event.id))
    if discord_errors:
        body["discord_errors"] = discord_errors
    return body


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(
    event_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
):
    event = await _load_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_write_access(db, user, tenant, event)
    before = _event_audit_snapshot(event)
    await remove_event_from_discord(db, discord, event.id)
    await drop_event_signup(db, event.id)
    await db.delete(event)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="events", row_id=event_id,
        action="delete", before=before,
    )
    await db.commit()
