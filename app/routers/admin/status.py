from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import DiscordConfig, SchedulerState
from services.discord_api import verify_token
from services.auth import require_admin_key

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.get("/api/status")
async def status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SchedulerState))
    states = {s.job_name: s for s in result.scalars().all()}

    cfg     = await db.execute(select(DiscordConfig))
    discord_cfg = cfg.scalar_one_or_none()
    discord_ok, discord_bot = False, None
    if discord_cfg:
        discord_ok, discord_bot = await verify_token(discord_cfg.bot_token)

    regen = states.get("regenerate_occurrences")
    notif = states.get("pre_event_notifier")

    return {
        "service":          "samaya",
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
