"""Manual scheduler triggers: preview upcoming occurrence dates for a
not-yet-saved event, and force an out-of-band regeneration run for the
current tenant only (the same regenerate_occurrences job scheduler/jobs.py
runs automatically for every tenant, every day at UTC 00:00).
"""
from datetime import date

from fastapi import APIRouter, Depends

from models.db import Tenant
from services.recurrence import next_occurrences
from scheduler.jobs import regenerate_occurrences
from services.auth import require_admin_key

from .deps import get_current_tenant
from .schemas import EventIn

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.post("/api/scheduler/preview")
async def preview_occurrences(payload: EventIn):
    anchor = date.fromisoformat(payload.anchor_date)
    dates  = next_occurrences(anchor, payload.interval_days, date.today(), count=10)
    return [{"date": str(d), "day": d.strftime("%A")} for d in dates]


@router.post("/api/scheduler/regenerate")
async def manual_regenerate(tenant: Tenant = Depends(get_current_tenant)):
    await regenerate_occurrences(tenant_id=tenant.id)
    return {"status": "ok", "message": "Regeneration complete"}
