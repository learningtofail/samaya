from datetime import date

from fastapi import APIRouter, Depends

from services.recurrence import next_occurrences
from scheduler.jobs import regenerate_occurrences
from services.auth import require_admin_key

from .schemas import EventIn

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.post("/api/scheduler/preview")
async def preview_occurrences(payload: EventIn):
    anchor = date.fromisoformat(payload.anchor_date)
    dates  = next_occurrences(anchor, payload.interval_days, date.today(), count=10)
    return [{"date": str(d), "day": d.strftime("%A")} for d in dates]


@router.post("/api/scheduler/regenerate")
async def manual_regenerate():
    await regenerate_occurrences()
    return {"status": "ok", "message": "Regeneration complete"}

