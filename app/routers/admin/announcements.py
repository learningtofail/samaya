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

from .deps import (
    check_kingdom_coordinator, get_current_tenant, get_current_tenants, require_not_viewer,
    get_current_user, resolve_target_tenants,
)
from .schemas import AnnouncementIn, AnnouncementPatch

router = APIRouter()


def _announcement_dict(a: Announcement, owning_tenant: Tenant | None = None) -> dict:
    d = {
        "id":               a.id,
        "owning_tenant_id": a.owning_tenant_id,
        "scope":            a.scope,
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
    # Spec §38.3 — the owning alliance's name/slug, for the "All" filter's
    # badge; only populated when the caller (list_announcements below)
    # actually looked it up, since every other caller of this dict-builder
    # is already scoped to one known tenant and has no need for it.
    if owning_tenant is not None:
        d["owning_tenant_slug"] = owning_tenant.slug
        d["owning_tenant_name"] = owning_tenant.name
        d["owning_tenant_color"] = owning_tenant.color
    return d


@router.get("/api/announcements")
async def list_announcements(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)
):
    """Spec §38.3: moved from get_current_tenant to get_current_tenants —
    every announcement targeting *any* accessible alliance, whether it's
    the owner or just one of the targets (a coordinator should see an
    announcement they're on the receiving end of, not only ones they
    authored). "X-Tenant-Slug: *" (the consolidated Announcements page's
    default) returns the union across every accessible alliance; a real
    slug narrows to that one alliance's targets, same as before this
    section."""
    tenant_ids = [t.id for t in tenants]
    tenants_by_id = {t.id: t for t in tenants}

    result = await db.execute(
        select(Announcement)
        .join(AnnouncementTarget, AnnouncementTarget.announcement_id == Announcement.id)
        .where(AnnouncementTarget.tenant_id.in_(tenant_ids))
        .options(selectinload(Announcement.targets))
        .order_by(Announcement.scheduled_for.desc())
    )
    announcements = result.unique().scalars().all()

    # owning_tenant_id may point at an alliance outside the accessible set
    # (e.g. a superadmin viewing combined mode sees an announcement they
    # were only cc'd on) — look those up too rather than leaving the badge
    # blank for that case.
    missing_owner_ids = {a.owning_tenant_id for a in announcements} - set(tenants_by_id)
    if missing_owner_ids:
        extra = await db.execute(select(Tenant).where(Tenant.id.in_(missing_owner_ids)))
        tenants_by_id.update({t.id: t for t in extra.scalars().all()})

    return [_announcement_dict(a, tenants_by_id.get(a.owning_tenant_id)) for a in announcements]


@router.post("/api/announcements", status_code=201)
async def create_announcement(
    payload: AnnouncementIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.scope == "kingdom-wide":
        await check_kingdom_coordinator(db, user, tenant.kingdom_id)

    tenants_by_slug = await resolve_target_tenants(db, user, [t.tenant_slug for t in payload.targets])

    try:
        scheduled_for = datetime.fromisoformat(payload.scheduled_for)
    except ValueError:
        raise HTTPException(status_code=422, detail="scheduled_for must be a valid ISO 8601 datetime")
    if scheduled_for <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="scheduled_for must be in the future")

    announcement = Announcement(
        owning_tenant_id=tenant.id, scope=payload.scope, title=payload.title, body_markdown=payload.body_markdown,
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


@router.patch("/api/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: int, payload: AnnouncementPatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Spec §49 — editing a still-scheduled announcement in place, instead
    of the previous cancel-then-recreate-from-scratch-or-Duplicate-only
    workflow. Only while status == 'scheduled': once it's posted (or
    failed/cancelled), the message already went out (or the row is
    terminal) and there's nothing left to edit — same reasoning
    delete_announcement already applies to its own terminal-state check.
    """
    result = await db.execute(
        select(Announcement)
        .options(selectinload(Announcement.targets))
        .where(Announcement.id == announcement_id, Announcement.owning_tenant_id == tenant.id)
    )
    announcement = result.scalar_one_or_none()
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")
    if announcement.status != "scheduled":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot edit an announcement with status '{announcement.status}' — only a scheduled one can be edited",
        )

    # Spec §49 — reassigning to a different owning alliance, same access
    # check (and same reasoning) as EventPatch.owning_tenant_slug.
    new_owner = None
    if payload.owning_tenant_slug is not None and payload.owning_tenant_slug != tenant.slug:
        owner_map = await resolve_target_tenants(db, user, [payload.owning_tenant_slug])
        new_owner = owner_map[payload.owning_tenant_slug]

    effective_tenant = new_owner or tenant
    target_scope = payload.scope if payload.scope is not None else announcement.scope
    if target_scope == "kingdom-wide":
        await check_kingdom_coordinator(db, user, effective_tenant.kingdom_id)

    targets_by_slug = None
    if payload.targets is not None:
        targets_by_slug = await resolve_target_tenants(db, user, [t.tenant_slug for t in payload.targets])

    scheduled_for = announcement.scheduled_for
    if payload.scheduled_for is not None:
        try:
            scheduled_for = datetime.fromisoformat(payload.scheduled_for)
        except ValueError:
            raise HTTPException(status_code=422, detail="scheduled_for must be a valid ISO 8601 datetime")
        if scheduled_for <= datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="scheduled_for must be in the future")

    before = {"title": announcement.title, "scope": announcement.scope, "owning_tenant_id": announcement.owning_tenant_id}

    if new_owner is not None:
        announcement.owning_tenant_id = new_owner.id
    if payload.title is not None:                announcement.title = payload.title
    if payload.body_markdown is not None:         announcement.body_markdown = payload.body_markdown
    announcement.scheduled_for = scheduled_for
    if payload.scope is not None:                 announcement.scope = payload.scope
    if payload.leadership_only is not None:       announcement.leadership_only = payload.leadership_only
    if payload.event_offset_minutes is not None:  announcement.event_offset_minutes = payload.event_offset_minutes
    if payload.recurring is not None:             announcement.recurring = payload.recurring
    if payload.interval_days is not None:         announcement.interval_days = payload.interval_days
    if announcement.recurring and not announcement.interval_days:
        raise HTTPException(status_code=422, detail="interval_days must be a positive integer when recurring is true")
    if not announcement.recurring:
        announcement.interval_days = None

    if targets_by_slug is not None:
        # Full replace, same delete-then-add ordering as update_event's
        # targets replacement (avoids uq_announcement_target tripping when
        # a tenant is re-targeted with the same (announcement_id,
        # tenant_id) pair within one flush).
        for old in list(announcement.targets):
            await db.delete(old)
        await db.flush()
        for t in payload.targets:
            db.add(AnnouncementTarget(
                announcement_id=announcement.id, tenant_id=targets_by_slug[t.tenant_slug].id,
                discord_channel_id=t.discord_channel_id,
            ))

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="announcements", row_id=announcement.id, action="update",
        before=before,
        after={"title": announcement.title, "scope": announcement.scope, "owning_tenant_id": announcement.owning_tenant_id},
    )
    await db.commit()
    result = await db.execute(
        select(Announcement)
        .where(Announcement.id == announcement.id)
        .options(selectinload(Announcement.targets))
    )
    return _announcement_dict(result.scalar_one())


@router.post("/api/announcements/{announcement_id}/cancel")
async def cancel_announcement(
    announcement_id: int,
    tenant: Tenant = Depends(require_not_viewer),
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
    tenant: Tenant = Depends(require_not_viewer),
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
    tenant: Tenant = Depends(require_not_viewer),
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
