"""Admin CRUD for EventDefinition rows: list, create, patch, deactivate,
and permanently delete. Not to be confused with routers/events.py, which
is the public-facing read-only events API.

permanent_delete_event nulls out PostLog.event_id (rather than cascading
the delete to PostLog) so posting history survives the event definition
being removed — see the comment inline for why.

List includes kingdom-wide events owned by *other* tenants in the same
Kingdom (read visibility only). Creating or editing a kingdom-wide event
additionally requires a UserKingdom grant (check_kingdom_coordinator) —
being trusted with one alliance's own settings doesn't imply being
trusted to post into every other alliance's Discord server.
"""
import csv
import io
from datetime import date, time as dtime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import get_db
from models.db import EventDefinition, EventTarget, PostLog, Tenant, User
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error

from .deps import (
    check_kingdom_coordinator, get_current_tenant, get_current_tenants, get_current_user,
    require_not_viewer, resolve_target_tenants,
)
from .schemas import EventIn, EventPatch, EventTenantNotificationIn
from .serializers import _event_dict

router = APIRouter()

# spec §32 — bulk import/export column order. Deliberately excludes `scope`
# and `targets`: a kingdom-wide event's automatic fan-out and an explicit
# EventTarget list are both multi-row, cross-tenant concepts that don't
# flatten cleanly into one CSV row per event, so bulk import only ever
# creates alliance-scope events with no extra targets — see §32's Out of
# Scope for the reasoning. Export is symmetric with import: the exact same
# column set, in the same order, so a round-trip (export, tweak in a
# spreadsheet, re-import) works without reshaping anything by hand.
_BULK_EVENT_COLUMNS = [
    "name", "interval_days", "start_time_utc", "duration_hours",
    "discord_channel", "description", "leadership_only", "anchor_date",
    "notification_channel_id", "notification_role_id", "notify_minutes_before",
]


@router.get("/api/events")
async def list_events(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)
):
    """Single-tenant behavior is unchanged (tenants == [that tenant]).
    Combined mode (X-Tenant-Slug: *, see deps.get_current_tenants) unions
    each accessible tenant's own events with kingdom-wide events visible
    to any of them, de-duplicating by event id since two tenants sharing
    a Kingdom would otherwise see the same kingdom-wide event twice.
    """
    tenant_ids = [t.id for t in tenants]
    kingdom_ids = {t.kingdom_id for t in tenants}
    result = await db.execute(
        select(EventDefinition)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .options(selectinload(EventDefinition.targets))
        .where(
            or_(
                EventDefinition.owning_tenant_id.in_(tenant_ids),
                (EventDefinition.scope == "kingdom-wide") & (Tenant.kingdom_id.in_(kingdom_ids)),
            )
        )
        .order_by(EventDefinition.name)
    )
    seen = {}
    for e in result.scalars().all():
        seen[e.id] = e
    return [_event_dict(e) for e in seen.values()]


@router.post("/api/events", status_code=201)
async def create_event(
    payload: EventIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.scope == "kingdom-wide":
        await check_kingdom_coordinator(db, user, tenant.kingdom_id)

    # Resolve/validate targets before touching EventDefinition at all, so
    # a bad target slug fails clean rather than leaving a half-created
    # event behind.
    targets_by_slug = await resolve_target_tenants(db, user, [t.tenant_slug for t in payload.targets])

    from datetime import time as dtime
    try:
        h, m = map(int, payload.start_time_utc.split(":"))
        t = dtime(h, m)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid start time: {e}")
    event = EventDefinition(
        owning_tenant_id        = tenant.id,
        scope                   = payload.scope,
        name                    = payload.name,
        interval_days           = payload.interval_days,
        start_time_utc          = t,
        duration_hours          = payload.duration_hours,
        discord_channel         = payload.discord_channel,
        description             = payload.description,
        leadership_only         = payload.leadership_only,
        anchor_date             = date.fromisoformat(str(payload.anchor_date)),
        active                  = True,
        notification_channel_id = payload.notification_channel_id,
        notification_role_id    = payload.notification_role_id,
        notify_minutes_before   = payload.notify_minutes_before,
        cover_image_data        = payload.cover_image_data or None,
    )
    event.targets = [
        EventTarget(
            tenant_id=targets_by_slug[t.tenant_slug].id,
            notification_channel_id=t.notification_channel_id,
            notification_role_id=t.notification_role_id,
        )
        for t in payload.targets
    ]
    db.add(event)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        # Both of these are already enforced at the Pydantic layer
        # (services/validators.py's parse_duration_hours/parse_interval_days)
        # before a request ever reaches the DB — this is a defense-in-depth
        # backstop, not something a normal request can trigger. The
        # anchor_date branch this replaced ("isoformat"/"anchor" substring
        # matching) was dead code regardless: date.fromisoformat() above
        # runs before this try block even starts, so a bad anchor_date was
        # never going to surface here as a caught exception in the first
        # place — it would have 500'd earlier, or never happened at all
        # (payload.anchor_date is already a real date by the time Pydantic
        # hands it to us).
        raise_friendly_integrity_error(e, {
            "ck_duration_positive": "Duration must be greater than 0",
            "ck_interval_positive": "Interval must be at least 1 day",
        })

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="event_definitions", row_id=event.id, action="create",
        after={"name": event.name, "scope": event.scope, "active": event.active},
    )
    await db.commit()
    await db.refresh(event)
    # commit() expires every attribute by default, including the `targets`
    # collection this function set in-memory above — without this, the
    # relationship access in _event_dict() would trigger a lazy load,
    # which raises under async SQLAlchemy rather than working silently.
    await db.refresh(event, attribute_names=["targets"])
    return _event_dict(event)


@router.patch("/api/events/{event_id}")
async def update_event(
    event_id: int, payload: EventPatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from datetime import time as dtime
    result = await db.execute(
        select(EventDefinition)
        # Eager-loaded because replacing event.targets below (when the
        # patch includes a targets list) needs the CURRENT collection
        # loaded first to diff old vs. new — an unloaded relationship
        # would otherwise lazy-load here, which raises under async
        # SQLAlchemy rather than working silently.
        .options(selectinload(EventDefinition.targets))
        .where(EventDefinition.id == event_id, EventDefinition.owning_tenant_id == tenant.id)
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Spec §49 — reassigning to a different owning alliance. The caller
    # must have access to the NEW tenant too (resolve_target_tenants runs
    # the same access check Announcements'/Events' explicit targets
    # already use) — being trusted with this event via the current
    # tenant's access doesn't imply being trusted to move it into a
    # completely different alliance.
    new_owner = None
    if payload.owning_tenant_slug is not None and payload.owning_tenant_slug != tenant.slug:
        owner_map = await resolve_target_tenants(db, user, [payload.owning_tenant_slug])
        new_owner = owner_map[payload.owning_tenant_slug]

    effective_tenant = new_owner or tenant
    target_scope = payload.scope if payload.scope is not None else event.scope
    if target_scope == "kingdom-wide":
        await check_kingdom_coordinator(db, user, effective_tenant.kingdom_id)

    # Resolve/validate before mutating the event — same reasoning as
    # create_event: a bad target slug should fail clean, not leave a
    # half-applied patch behind.
    targets_by_slug = None
    if payload.targets is not None:
        targets_by_slug = await resolve_target_tenants(db, user, [t.tenant_slug for t in payload.targets])

    before = {"name": event.name, "scope": event.scope, "active": event.active, "owning_tenant_id": event.owning_tenant_id}

    if new_owner is not None:
        event.owning_tenant_id = new_owner.id
        # discord_channel/notification_channel_id/notification_role_id are
        # real Discord snowflake IDs tied to one guild — carrying them over
        # onto a different alliance's (possibly different) server would be
        # silently wrong, so they're cleared unless the two alliances
        # actually share a Discord server (Tenant.server_id, spec §25).
        if new_owner.server_id != tenant.server_id:
            event.discord_channel = ""
            event.notification_channel_id = ""
            event.notification_role_id = ""

    if payload.name is not None:                    event.name                    = payload.name
    if payload.interval_days is not None:           event.interval_days           = payload.interval_days
    if payload.duration_hours is not None:          event.duration_hours          = payload.duration_hours
    if payload.discord_channel is not None:         event.discord_channel         = payload.discord_channel
    if payload.description is not None:             event.description             = payload.description
    if payload.scope is not None:                   event.scope                   = payload.scope
    if payload.leadership_only is not None:         event.leadership_only         = payload.leadership_only
    if payload.active is not None:                  event.active                  = payload.active
    if payload.anchor_date is not None:             event.anchor_date             = date.fromisoformat(payload.anchor_date)
    if payload.notification_channel_id is not None: event.notification_channel_id = payload.notification_channel_id
    if payload.notification_role_id is not None:    event.notification_role_id    = payload.notification_role_id
    if payload.notify_minutes_before is not None:   event.notify_minutes_before   = payload.notify_minutes_before
    if payload.cover_image_data is not None:        event.cover_image_data        = payload.cover_image_data or None
    if payload.start_time_utc is not None:
        try:
            h, m = map(int, payload.start_time_utc.split(":"))
            event.start_time_utc = dtime(h, m)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"Invalid start time: {e}")

    if targets_by_slug is not None:
        # Full replace, not a merge — the modal always shows and saves
        # back the complete target list (see events.js's openEventModal/
        # saveEvent), so there's no partial-update case to reconcile here.
        # Explicit delete-then-add rather than reassigning event.targets
        # wholesale: SQLAlchemy's collection-replacement diffing can
        # schedule the new rows' INSERT before the old rows' DELETE
        # within the same flush, which trips uq_event_target when a
        # tenant is re-targeted with the same (event_id, tenant_id) pair.
        for old in list(event.targets):
            await db.delete(old)
        await db.flush()
        for t in payload.targets:
            db.add(EventTarget(
                event_id=event.id, tenant_id=targets_by_slug[t.tenant_slug].id,
                notification_channel_id=t.notification_channel_id,
                notification_role_id=t.notification_role_id,
            ))

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="event_definitions", row_id=event.id, action="update",
        before=before,
        after={"name": event.name, "scope": event.scope, "active": event.active, "owning_tenant_id": event.owning_tenant_id},
    )
    await db.commit()
    await db.refresh(event)
    await db.refresh(event, attribute_names=["targets"])
    return _event_dict(event)


@router.delete("/api/events/{event_id}", status_code=204)
async def deactivate_event(
    event_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EventDefinition).where(
            EventDefinition.id == event_id, EventDefinition.owning_tenant_id == tenant.id
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.active = False
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="event_definitions", row_id=event.id, action="update",
        before={"active": True}, after={"active": False},
    )
    await db.commit()


@router.put("/api/events/{event_id}/notification-override")
async def set_notification_override(
    event_id: int, payload: EventTenantNotificationIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A non-owning tenant's own notification channel/role for a
    kingdom-wide event — see models.db.EventTenantNotification. Always
    applies to the caller's own current tenant; there is no way to set
    another tenant's override through this endpoint.

    Deliberately does not require check_kingdom_coordinator: configuring
    where *your own* alliance gets pinged for an event someone else
    created is a lower-stakes action than creating the kingdom-wide event
    itself, and gating it the same way would mean an alliance's regular
    coordinators can never set this up without asking their kingdom
    coordinator to do it for them every time.
    """
    from models.db import EventDefinition, EventTenantNotification

    result = await db.execute(
        select(EventDefinition)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .where(
            EventDefinition.id == event_id,
            EventDefinition.scope == "kingdom-wide",
            Tenant.kingdom_id == tenant.kingdom_id,
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Kingdom-wide event not found in your kingdom")

    if tenant.id == event.owning_tenant_id:
        raise HTTPException(
            status_code=400,
            detail="The owning tenant uses the event's own notification_channel_id/role — "
                   "set those via PATCH /api/events/{event_id} instead",
        )

    existing = await db.execute(
        select(EventTenantNotification).where(
            EventTenantNotification.event_id == event_id,
            EventTenantNotification.tenant_id == tenant.id,
        )
    )
    override = existing.scalar_one_or_none()
    if override:
        override.notification_channel_id = payload.notification_channel_id
        override.notification_role_id    = payload.notification_role_id
    else:
        override = EventTenantNotification(
            event_id=event_id, tenant_id=tenant.id,
            notification_channel_id=payload.notification_channel_id,
            notification_role_id=payload.notification_role_id,
        )
        db.add(override)

    await db.commit()
    return {
        "event_id": event_id, "tenant_id": tenant.id,
        "notification_channel_id": override.notification_channel_id,
        "notification_role_id": override.notification_role_id,
    }


@router.delete("/api/events/{event_id}/permanent", status_code=200)
async def permanent_delete_event(
    event_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EventDefinition).where(
            EventDefinition.id == event_id, EventDefinition.owning_tenant_id == tenant.id
        )
    )
    event = result.scalar_one_or_none()
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

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="event_definitions", row_id=event.id, action="delete",
        before={"name": event.name, "scope": event.scope},
    )

    # Delete the event — cascades to occurrences
    await db.delete(event)
    await db.commit()

    return {
        "status": "deleted",
        "event_name": event.name,
        "post_log_entries_preserved": len(log_entries),
    }


@router.get("/api/events/export.csv")
async def export_events_csv(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    """spec §32 — bulk export, paired with import below. Alliance-scope
    events owned by the current tenant only: a kingdom-wide event's
    automatic fan-out and any explicit EventTarget rows aren't
    representable in one flat CSV row, so this (deliberately) leaves them
    out rather than producing a lossy or misleading export of them — see
    _BULK_EVENT_COLUMNS' comment."""
    result = await db.execute(
        select(EventDefinition).where(
            EventDefinition.owning_tenant_id == tenant.id,
            EventDefinition.scope == "alliance",
        ).order_by(EventDefinition.name)
    )
    events = result.scalars().all()

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_BULK_EVENT_COLUMNS)
        yield buf.getvalue()
        for e in events:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow([
                e.name, e.interval_days, e.start_time_utc.strftime("%H:%M"),
                str(e.duration_hours), e.discord_channel, e.description,
                str(e.leadership_only).lower(), e.anchor_date.isoformat(),
                e.notification_channel_id, e.notification_role_id,
                e.notify_minutes_before if e.notify_minutes_before is not None else "",
            ])
            yield buf.getvalue()

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=Events_Export.csv"},
    )


@router.post("/api/events/import.csv")
async def import_events_csv(
    file: UploadFile,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The import half of §32's bulk import/export pair. Every row becomes
    a brand-new alliance-scope event owned by the current tenant — this is
    additive only, never an update-by-name or upsert, so re-importing the
    same file twice creates duplicates rather than silently overwriting
    anything (matching how every other create endpoint in this app works:
    there's no merge-by-name convention anywhere else to be consistent
    with). A bad row doesn't abort the whole file: each row is validated
    independently through EventIn's own validators (the same rules the
    single-event create form enforces), valid rows are created, and every
    invalid row is reported back by its 1-indexed CSV row number (header
    row is row 1, so the first data row is row 2 — matching what a
    coordinator sees if they open the file in a spreadsheet) with the
    validation error, rather than the caller needing to fix a file blind
    and re-upload it as guesswork.
    """
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    missing_cols = set(_BULK_EVENT_COLUMNS) - set(reader.fieldnames or [])
    if missing_cols:
        raise HTTPException(
            status_code=422,
            detail=f"CSV is missing required column(s): {sorted(missing_cols)}",
        )

    created = []
    errors = []
    for row_num, row in enumerate(reader, start=2):
        try:
            payload = EventIn(
                name=row["name"],
                interval_days=row["interval_days"],
                start_time_utc=row["start_time_utc"],
                duration_hours=row["duration_hours"],
                discord_channel=row.get("discord_channel") or "",
                description=row.get("description") or "",
                leadership_only=str(row.get("leadership_only", "")).strip().lower() in ("true", "1", "yes"),
                scope="alliance",
                anchor_date=row["anchor_date"],
                notification_channel_id=row.get("notification_channel_id") or "",
                notification_role_id=row.get("notification_role_id") or "",
                notify_minutes_before=row.get("notify_minutes_before") or None,
                targets=[],
            )
        except ValidationError as e:
            errors.append({"row": row_num, "detail": "; ".join(err["msg"] for err in e.errors())})
            continue

        event = EventDefinition(
            owning_tenant_id        = tenant.id,
            scope                   = "alliance",
            name                    = payload.name,
            interval_days           = payload.interval_days,
            start_time_utc          = dtime(*map(int, payload.start_time_utc.split(":"))),
            duration_hours          = payload.duration_hours,
            discord_channel         = payload.discord_channel,
            description             = payload.description,
            leadership_only         = payload.leadership_only,
            anchor_date             = date.fromisoformat(str(payload.anchor_date)),
            active                  = True,
            notification_channel_id = payload.notification_channel_id,
            notification_role_id    = payload.notification_role_id,
            notify_minutes_before   = payload.notify_minutes_before,
        )
        db.add(event)
        try:
            await db.flush()
        except Exception as e:
            await db.rollback()
            errors.append({"row": row_num, "detail": f"Database error: {e}"})
            continue
        created.append(event)

    for event in created:
        await log_change(
            db, user_id=user.id, tenant_id=tenant.id,
            table_name="event_definitions", row_id=event.id, action="create",
            after={"name": event.name, "scope": event.scope, "active": event.active, "source": "bulk_import"},
        )
    await db.commit()

    return {"created": len(created), "errors": errors}
