"""GET /admin/api/status — scheduler + Discord connectivity summary shown
on the admin dashboard's top bar.

GET /admin/api/delivery-health (spec §31) — a trailing-7-day rollup of
delivery success/failure across both channels this app posts through:
AnnouncementTarget (spec §13) and PostLog (event Discord posts).

Spec §38.2: both moved from get_current_tenant to get_current_tenants —
"X-Tenant-Slug: *" (the default in the new consolidated Dashboard) returns
one row per accessible alliance instead of one tenant's numbers, so a
kingdom-wide view doesn't collapse everyone's health into a single
misleading sum. Narrowing the Dashboard's filter to one alliance still
sends its real slug, in which case each list below simply has one entry —
same response shape either way, no separate "single" vs "combined" branch
for callers to handle.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Announcement, AnnouncementTarget, PostLog, SchedulerState, Tenant
from services.discord_api import verify_token

from .deps import PLATFORM_BOT_TOKEN, get_current_tenants

router = APIRouter()


async def _status_for_tenant(tenant: Tenant, db: AsyncSession) -> dict:
    result = await db.execute(select(SchedulerState).where(SchedulerState.tenant_id == tenant.id))
    states = {s.job_name: s for s in result.scalars().all()}

    token = tenant.server.bot_token or PLATFORM_BOT_TOKEN
    discord_ok, discord_bot = (False, None)
    if token:
        discord_ok, discord_bot = await verify_token(token)

    regen = states.get("regenerate_occurrences")
    notif = states.get("pre_event_notifier")

    return {
        "tenant_slug":       tenant.slug,
        "tenant_name":       tenant.name,
        "tenant_color":      tenant.color,
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
    }


@router.get("/api/status")
async def status(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)
):
    alliances = [await _status_for_tenant(t, db) for t in tenants]
    return {
        "service":   "samaya",
        "database":  "ok",
        "alliances": alliances,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _delivery_health_for_tenant(tenant: Tenant, db: AsyncSession, since: datetime) -> dict:
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
        "tenant_slug":  tenant.slug,
        "tenant_name":  tenant.name,
        "tenant_color": tenant.color,
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


@router.get("/api/delivery-health")
async def delivery_health(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)
):
    since = datetime.now(timezone.utc) - timedelta(days=7)
    alliances = [await _delivery_health_for_tenant(t, db, since) for t in tenants]
    return {
        "window_days": 7,
        "alliances":   alliances,
    }
