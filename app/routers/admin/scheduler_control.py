"""Manual scheduler triggers: preview upcoming occurrence dates for a
not-yet-saved event, and force an out-of-band regeneration run for the
current tenant only (the same regenerate_occurrences job scheduler/regeneration.py
runs automatically for every tenant, every day at UTC 00:00).
"""
from datetime import date

from fastapi import APIRouter, Depends

from models import get_session_factory
from models.db import Tenant
from services.recurrence import next_occurrences
from scheduler.regeneration import regenerate_occurrences

from .deps import get_current_tenant
from .schemas import EventIn

router = APIRouter()


@router.post("/api/scheduler/preview")
async def preview_occurrences(payload: EventIn):
    anchor = date.fromisoformat(payload.anchor_date)
    dates  = next_occurrences(anchor, payload.interval_days, date.today(), count=10)
    return [{"date": str(d), "day": d.strftime("%A")} for d in dates]


@router.post("/api/scheduler/regenerate")
async def manual_regenerate(
    tenant: Tenant = Depends(get_current_tenant),
    session_factory=Depends(get_session_factory),
):
    # regenerate_occurrences opens its own session via session_factory
    # rather than accepting this route's own `db` session — passing it
    # explicitly (instead of letting it default to the real
    # AsyncSessionLocal) is what makes this endpoint's DB access actually
    # honor get_db's override in tests, the same way every other route's
    # own `db: AsyncSession = Depends(get_db)` already does.
    await regenerate_occurrences(tenant_id=tenant.id, session_factory=session_factory)
    return {"status": "ok", "message": "Regeneration complete"}
