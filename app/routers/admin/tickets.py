"""Admin triage view over the public feedback board's Ticket rows (spec
§43.3). Kingdom-wide, not tenant-scoped by query — a ticket's own
tenant_id is informational (who it's about/for), but every admin with
access to at least one tenant sees the whole list, same "cross-alliance
visibility is the point" reasoning already applied to the Audit Log.

GET requires only that the caller be logged in with access to some
tenant (get_current_tenant, X-Tenant-Slug header still required to prove
that, even though the ticket query itself ignores which one); PATCH
(status changes) additionally requires require_not_viewer, matching
every other mutating admin route in this app (spec §31.3).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Announcement, EventDefinition, Occurrence, Tenant, Ticket

from .deps import get_current_tenant, require_not_viewer

router = APIRouter()

_STATUSES = ("open", "planned", "in_progress", "done", "declined")


def _ticket_admin_dict(t: Ticket, tenant_by_id: dict, occ_by_id: dict, ann_by_id: dict) -> dict:
    related_name = None
    if t.related_occurrence_id and t.related_occurrence_id in occ_by_id:
        related_name = occ_by_id[t.related_occurrence_id]
    elif t.related_announcement_id and t.related_announcement_id in ann_by_id:
        related_name = ann_by_id[t.related_announcement_id]

    tenant = tenant_by_id.get(t.tenant_id) if t.tenant_id else None
    return {
        "id": t.id,
        "kind": t.kind,
        "error_type": t.error_type,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "upvote_count": t.upvote_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "tenant_name": tenant.name if tenant else None,
        "tenant_slug": tenant.slug if tenant else None,
        "related_name": related_name,
        "submitter_contact": t.submitter_contact,
    }


@router.get("/api/tickets")
async def list_tickets_admin(tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Ticket).order_by(Ticket.upvote_count.desc(), Ticket.created_at.desc()))
    tickets = result.scalars().all()

    tenant_ids = {t.tenant_id for t in tickets if t.tenant_id}
    tenant_by_id = {}
    if tenant_ids:
        tres = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
        tenant_by_id = {t.id: t for t in tres.scalars().all()}

    occ_ids = {t.related_occurrence_id for t in tickets if t.related_occurrence_id}
    occ_by_id = {}
    if occ_ids:
        ores = await db.execute(
            select(Occurrence.id, EventDefinition.name)
            .join(EventDefinition, Occurrence.event_id == EventDefinition.id)
            .where(Occurrence.id.in_(occ_ids))
        )
        occ_by_id = {row[0]: row[1] for row in ores.all()}

    ann_ids = {t.related_announcement_id for t in tickets if t.related_announcement_id}
    ann_by_id = {}
    if ann_ids:
        ares = await db.execute(select(Announcement.id, Announcement.title).where(Announcement.id.in_(ann_ids)))
        ann_by_id = {row[0]: row[1] for row in ares.all()}

    return [_ticket_admin_dict(t, tenant_by_id, occ_by_id, ann_by_id) for t in tickets]


class TicketStatusPatch(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v):
        if v not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}")
        return v


@router.patch("/api/tickets/{ticket_id}")
async def update_ticket_status(
    ticket_id: int, payload: TicketStatusPatch,
    tenant: Tenant = Depends(require_not_viewer), db: AsyncSession = Depends(get_db),
):
    ticket = await db.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket.status = payload.status
    await db.commit()
    await db.refresh(ticket)
    return {"id": ticket.id, "status": ticket.status}
