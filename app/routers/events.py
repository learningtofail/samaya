from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, Occurrence, Tenant

router = APIRouter()

WINDOW_DAYS = 28


async def _get_tenant_by_slug(tenant_slug: str, db: AsyncSession) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail=f"No such alliance: {tenant_slug}")
    return tenant


@router.get("/t/{tenant_slug}/api/events")
async def list_events(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    tenant = await _get_tenant_by_slug(tenant_slug, db)
    today = date.today()
    end   = today + timedelta(days=WINDOW_DAYS)

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
    rows = result.all()

    return JSONResponse([
        {
            "id":                 occ.id,
            "event_name":         event.name,
            "scope":              event.scope,
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


@router.get("/t/{tenant_slug}/events", response_class=HTMLResponse)
async def events_page(tenant_slug: str, db: AsyncSession = Depends(get_db)):
    await _get_tenant_by_slug(tenant_slug, db)  # 404s early for an unknown slug
    with open("/app/static/events.html") as f:
        return HTMLResponse(f.read())
