"""Admin CRUD for EventDefinition rows: list, create, patch, deactivate,
and permanently delete. Not to be confused with routers/events.py, which
is the public-facing read-only events API.

permanent_delete_event nulls out PostLog.event_id (rather than cascading
the delete to PostLog) so posting history survives the event definition
being removed — see the comment inline for why.

List includes kingdom-wide events owned by *other* tenants in the same
Kingdom (read visibility only — mutations below stay scoped to events
this tenant actually owns). Editing another tenant's kingdom-wide event
requires a UserKingdom grant, which doesn't exist yet (Phase 4); during
the bridge period every mutation here is scoped to owning_tenant_id ==
the caller's own tenant, same as an alliance-scoped event always was.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, PostLog, Tenant
from services.auth import require_admin_key

from .deps import get_current_tenant
from .schemas import EventIn, EventPatch
from .serializers import _event_dict

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.get("/api/events")
async def list_events(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(EventDefinition)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .where(
            or_(
                EventDefinition.owning_tenant_id == tenant.id,
                (EventDefinition.scope == "kingdom-wide") & (Tenant.kingdom_id == tenant.kingdom_id),
            )
        )
        .order_by(EventDefinition.name)
    )
    return [_event_dict(e) for e in result.scalars().all()]


@router.post("/api/events", status_code=201)
async def create_event(
    payload: EventIn, tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
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
async def update_event(
    event_id: int, payload: EventPatch,
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db),
):
    from datetime import time as dtime
    result = await db.execute(
        select(EventDefinition).where(
            EventDefinition.id == event_id, EventDefinition.owning_tenant_id == tenant.id
        )
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

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
async def deactivate_event(
    event_id: int, tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
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
    await db.commit()


@router.delete("/api/events/{event_id}/permanent", status_code=200)
async def permanent_delete_event(
    event_id: int, tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
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

    # Delete the event — cascades to occurrences
    await db.delete(event)
    await db.commit()

    return {
        "status": "deleted",
        "event_name": event.name,
        "post_log_entries_preserved": len(log_entries),
    }
