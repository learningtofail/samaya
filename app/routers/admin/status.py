"""GET /admin/api/status — scheduler + Discord connectivity summary shown
on the admin dashboard's top bar, scoped to the current tenant
(services/discord_api.verify_token, models.db.SchedulerState).

GET /admin/api/delivery-health (spec §31) — a trailing-7-day rollup of
delivery success/failure across both channels this app posts through:
AnnouncementTarget (spec §13) and PostLog (event Discord posts). One
tenant's numbers only, same scoping as everything else here — a
coordinator cares about whether *their* messages are landing, not the
whole deployment's.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Announcement, AnnouncementTarget, PostLog, SchedulerState, Tenant
from services.discord_api import verify_token

from .deps import PLATFORM_BOT_TOKEN, get_current_tenant

router = APIRouter()


@router.get("/api/status")
async def status(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(SchedulerState).where(SchedulerState.tenant_id == tenant.id))
    states = {s.job_name: s for s in result.scalars().all()}

    token = tenant.server.bot_token or PLATFORM_BOT_TOKEN
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


@router.get("/api/delivery-health")
async def delivery_health(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    since = datetime.now(timezone.utc) - timedelta(days=7)

    # Announcement deliveries to this tenant — post_status lives on the
    # per-target row, but the only timestamp available is the parent
    # Announcement's posted_at (set once the whole announcement goes
    # terminal), so the window is applied there rather than per-target.
    ann_result = await db.execute(
        select(AnnouncementTarget.post_status, func.count())
        .join(Announcement, Announcement.id == AnnouncementTarget.announcement_id)
        .where(AnnouncementTarget.tenant_id == tenant.id, Announcement.posted_at >= since)
        .group_by(AnnouncementTarget.post_status)
    )
    ann_counts = dict(ann_result.all())

    # Event Discord posts to this tenant — PostLog carries its own
    # per-row posted_at_utc, so the window applies directly.
    log_result = await db.execute(
        select(PostLog.status, func.count())
        .where(PostLog.tenant_id == tenant.id, PostLog.posted_at_utc >= since)
        .group_by(PostLog.status)
    )
    log_counts = dict(log_result.all())

    return {
        "window_days": 7,
        "announcements": {
            "posted": ann_counts.get("posted", 0),
            "error":  ann_counts.get("error", 0),
        },
        "event_posts": {
            "posted":    log_counts.get("posted", 0),
            "error":     log_counts.get("error", 0),
            "cancelled": log_counts.get("cancelled", 0),
        },
    }
