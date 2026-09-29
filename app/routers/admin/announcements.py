"""Scheduled announcements: create/list/cancel. Delivery itself happens
in scheduler/jobs.py's per-minute tick, reusing services/discord_api's
send_channel_message — the same function that posts the "event has been
scheduled" ping for occurrences (see routers/admin/occurrences.py).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import get_db
from models.db import Announcement, AnnouncementTarget, Tenant, User
from services.audit import log_change

from .deps import get_current_tenant, get_current_user, resolve_target_tenants
from .schemas import AnnouncementIn

router = APIRouter()


def _announcement_dict(a: Announcement) -> dict:
    return {
        "id":               a.id,
        "owning_tenant_id": a.owning_tenant_id,
        "title":            a.title,
        "body_markdown":    a.body_markdown,
        "scheduled_for":    a.scheduled_for.isoformat(),
        "status":           a.status,
        "leadership_only":  a.leadership_only,
        "recurring":        a.recurring,
        "interval_days":    a.interval_days,
        "event_offset_minutes": a.event_offset_minutes,
        "posted_at":        a.posted_at.isoformat() if a.posted_at else None,
        "targets": [
            {
                "tenant_id":          t.tenant_id,
                "discord_channel_id": t.discord_channel_id,
                "discord_message_id": t.discord_message_id,
                "post_status":        t.post_status,
                "status_detail":      t.status_detail,
            }
            for t in a.targets
        ],
    }


@router.get("/api/announcements")
async def list_announcements(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    """This tenant's own announcements, whether it's the owner or just
    one of the targets — a coordinator should see an announcement
    they're on the receiving end of, not only ones they authored."""
    result = await db.execute(
        select(Announcement)
        .join(AnnouncementTarget, AnnouncementTarget.announcement_id == Announcement.id)
        .where(AnnouncementTarget.tenant_id == tenant.id)
        .options(selectinload(Announcement.targets))
        .order_by(Announcement.scheduled_for.desc())
    )
    announcements = result.unique().scalars().all()
    return [_announcement_dict(a) for a in announcements]


@router.post("/api/announcements", status_code=201)
async def create_announcement(
    payload: AnnouncementIn,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenants_by_slug = await resolve_target_tenants(db, user, [t.tenant_slug for t in payload.targets])

    try:
        scheduled_for = datetime.fromisoformat(payload.scheduled_for)
    except ValueError:
        raise HTTPException(status_code=422, detail="scheduled_for must be a valid ISO 8601 datetime")
    if scheduled_for <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="scheduled_for must be in the future")

    announcement = Announcement(
        owning_tenant_id=tenant.id, title=payload.title, body_markdown=payload.body_markdown,
        scheduled_for=scheduled_for, status="scheduled", created_by=user.id,
        leadership_only=payload.leadership_only, recurring=payload.recurring,
        interval_days=payload.interval_days, event_offset_minutes=payload.event_offset_minutes,
    )
    db.add(announcement)
    await db.flush()

    for target_in in payload.targets:
        db.add(AnnouncementTarget(
            announcement_id=announcement.id,
            tenant_id=tenants_by_slug[target_in.tenant_slug].id,
            discord_channel_id=target_in.discord_channel_id,
        ))

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="announcements", row_id=announcement.id, action="create",
        after={"title": announcement.title, "scheduled_for": payload.scheduled_for,
               "targets": [t.tenant_slug for t in payload.targets]},
    )
    await db.commit()
    result = await db.execute(
        select(Announcement)
        .where(Announcement.id == announcement.id)
        .options(selectinload(Announcement.targets))
    )
    announcement = result.scalar_one()
    return _announcement_dict(announcement)


@router.post("/api/announcements/{announcement_id}/cancel")
async def cancel_announcement(
    announcement_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Announcement).where(
            Announcement.id == announcement_id, Announcement.owning_tenant_id == tenant.id
        )
    )
    announcement = result.scalar_one_or_none()
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")
    if announcement.status != "scheduled":
        raise HTTPException(status_code=400, detail=f"Cannot cancel an announcement with status '{announcement.status}'")

    announcement.status = "cancelled"
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="announcements", row_id=announcement.id, action="update",
        before={"status": "scheduled"}, after={"status": "cancelled"},
    )
    await db.commit()
    return {"status": "cancelled"}


@router.post("/api/announcements/{announcement_id}/retry-failed-targets")
async def retry_failed_targets(
    announcement_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Requeues every errored AnnouncementTarget on this announcement so
    the next per-minute delivery tick (scheduler/announcements.py) picks
    it back up — nothing currently does this on its own for a one-time
    announcement (spec §30). A *recurring* announcement already
    self-heals on its next re-arm (every target, including errored ones,
    gets reset to pending), so this mainly matters for a one-time
    announcement that ended in 'failed' or 'posted' with some targets
    still in error.
    """
    result = await db.execute(
        select(Announcement)
        .where(Announcement.id == announcement_id, Announcement.owning_tenant_id == tenant.id)
        .options(selectinload(Announcement.targets))
    )
    announcement = result.scalar_one_or_none()
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")

    failed = [t for t in announcement.targets if t.post_status == "error"]
    if not failed:
        raise HTTPException(status_code=400, detail="No failed targets to retry")

    for t in failed:
        t.post_status = "pending"
        t.status_detail = None
        t.discord_message_id = None

    before_status = announcement.status
    # 'scheduled' is the only status the delivery tick's due-announcement
    # query looks at — a 'failed'/'posted' announcement with newly-pending
    # targets needs to go back to 'scheduled' or the next tick will never
    # look at it again. scheduled_for is already <= now (it already fired
    # once), so this picks the retried targets up on the very next tick.
    announcement.status = "scheduled"

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="announcements", row_id=announcement.id, action="update",
        before={"status": before_status, "retried_targets": [t.tenant_id for t in failed]},
        after={"status": "scheduled"},
    )
    await db.commit()
    result = await db.execute(
        select(Announcement)
        .where(Announcement.id == announcement.id)
        .options(selectinload(Announcement.targets))
    )
    return _announcement_dict(result.scalar_one())


@router.delete("/api/announcements/{announcement_id}", status_code=200)
async def delete_announcement(
    announcement_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Removes a finished announcement from the list outright — the
    cleanup mechanism a "scheduled" one doesn't need (cancel it first;
    see cancel_announcement above) but a posted/failed/cancelled one has
    no other way to leave, and the list otherwise only grows."""
    result = await db.execute(
        select(Announcement).where(
            Announcement.id == announcement_id, Announcement.owning_tenant_id == tenant.id
        )
    )
    announcement = result.scalar_one_or_none()
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")
    if announcement.status not in ("posted", "failed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete an announcement with status '{announcement.status}' — cancel it first",
        )

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="announcements", row_id=announcement.id, action="delete",
        before={"title": announcement.title, "status": announcement.status},
    )
    await db.delete(announcement)  # AnnouncementTarget rows cascade via ondelete="CASCADE"
    await db.commit()
    return {"status": "deleted"}
