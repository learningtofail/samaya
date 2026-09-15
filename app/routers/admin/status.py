"""GET /admin/api/status — scheduler + Discord connectivity summary shown
on the admin dashboard's top bar, scoped to the current tenant
(services/discord_api.verify_token, models.db.SchedulerState).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import SchedulerState, Tenant
from services.discord_api import verify_token

from .deps import PLATFORM_BOT_TOKEN, get_current_tenant

router = APIRouter()


@router.get("/api/status")
async def status(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(SchedulerState).where(SchedulerState.tenant_id == tenant.id))
    states = {s.job_name: s for s in result.scalars().all()}

    token = tenant.bot_token or PLATFORM_BOT_TOKEN
    discord_ok, discord_bot = (False, None)
    if token:
        discord_ok, discord_bot = await verify_token(token)

    regen = states.get("regenerate_occurrences")
    notif = states.get("pre_event_notifier")

    return {
        "service":          "samaya",
        "tenant":            tenant.slug,
        "database":         "ok",
        "discord_connected": discord_ok,
        "discord_bot":       discord_bot,
        "scheduler": {
            "regenerate_occurrences": {
                "last_run":    regen.last_run_utc.isoformat() if regen and regen.last_run_utc else None,
                "last_result": regen.last_result if regen else None,
                "last_detail": regen.last_detail if regen else None,
            },
            "pre_event_notifier": {
                "last_run":    notif.last_run_utc.isoformat() if notif and notif.last_run_utc else None,
                "last_result": notif.last_result if notif else None,
            },
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
