"""Admin moderation of the public feedback board (spec §43.3, §66.10).

Kingdom-wide, not tenant-scoped by query: every admin with access to at
least one tenant sees the whole board (X-Tenant-Slug still proves that).
Viewers see everything and change nothing (§31.3). Coordinators and owners
can respond, dismiss and edit; permanent deletion is superadmin only and is
always audited. Edits are audited with before and after, and the submitter
is not told.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import get_db
from models.db import Tenant, Ticket, TicketResponse, User
from services.audit import log_change
from services.ticket_board import BOARD_STATUSES, place
from services.ticket_views import (
    ALL_STATUSES, RESPONSE_MAX_CHARS, checklist_dict, occurrence_names, response_dict, tag_dict,
)

from .deps import get_current_tenant, get_current_user, require_not_viewer, require_superadmin

router = APIRouter()

_KINDS = ("feedback", "event_request", "announcement_request", "error")
_TITLE_MAX = 120
_DESCRIPTION_MAX = 2000
_NOTES_MAX = 5000


def _ticket_admin_dict(t: Ticket, tenant_by_id: dict, occ_names: dict) -> dict:
    tenant = tenant_by_id.get(t.tenant_id) if t.tenant_id else None
    return {
        "id": t.id,
        "kind": t.kind,
        "error_type": t.error_type,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "position": t.position,
        "internal_notes": t.internal_notes,
        "checklist": [checklist_dict(i) for i in t.checklist],
        "tags": [tag_dict(g) for g in t.tags],
        "upvote_count": t.upvote_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "tenant_name": tenant.name if tenant else None,
        "tenant_slug": tenant.slug if tenant else None,
        "related_occurrence_id": t.related_occurrence_id,
        "related_name": occ_names.get(t.related_occurrence_id),
        "submitter_contact": t.submitter_contact,
        "responses": [
            {**response_dict(r), "author_user_id": r.author_user_id} for r in t.responses
        ],
    }


def _audit_snapshot(t: Ticket) -> dict:
    return {"kind": t.kind, "error_type": t.error_type, "title": t.title,
            "description": t.description, "status": t.status, "internal_notes": t.internal_notes}


async def _load_ticket(db: AsyncSession, ticket_id: int) -> Ticket:
    ticket = (await db.execute(
        select(Ticket).options(selectinload(Ticket.responses), selectinload(Ticket.checklist), selectinload(Ticket.tags))
        .where(Ticket.id == ticket_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


async def _ticket_json(db: AsyncSession, ticket: Ticket) -> dict:
    tenant = await db.get(Tenant, ticket.tenant_id) if ticket.tenant_id else None
    return _ticket_admin_dict(ticket, {tenant.id: tenant} if tenant else {}, await occurrence_names(db, [ticket]))


@router.get("/api/tickets")
async def list_tickets_admin(tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Every ticket including dismissed ones, with its responses."""
    tickets = (await db.execute(
        select(Ticket).options(selectinload(Ticket.responses), selectinload(Ticket.checklist), selectinload(Ticket.tags))
        .order_by(Ticket.upvote_count.desc(), Ticket.created_at.desc(), Ticket.id.desc())
    )).scalars().unique().all()
    tenant_ids = {t.tenant_id for t in tickets if t.tenant_id}
    tenant_by_id = {}
    if tenant_ids:
        tenant_by_id = {t.id: t for t in (await db.execute(
            select(Tenant).where(Tenant.id.in_(tenant_ids)))).scalars().unique().all()}
    occ_names = await occurrence_names(db, tickets)
    return [_ticket_admin_dict(t, tenant_by_id, occ_names) for t in tickets]


class TicketPatch(BaseModel):
    """Status, or a correction to the text (to remove personal details or fix
    a typo) and the type. Only fields present are applied."""
    status:      Optional[str] = None
    title:       Optional[str] = Field(default=None, max_length=_TITLE_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)
    kind:        Optional[str] = None
    error_type:  Optional[str] = None
    internal_notes: Optional[str] = Field(default=None, max_length=_NOTES_MAX)

    @field_validator("internal_notes")
    @classmethod
    def _blank_notes_clear(cls, v):
        return (v.strip() or None) if v is not None else v

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v):
        if v is not None and v not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}")
        return v

    @field_validator("title", "description")
    @classmethod
    def _not_blank(cls, v):
        if v is not None and not v.strip():
            raise ValueError("must not be blank")
        return v.strip() if v is not None else v


@router.patch("/api/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: int, payload: TicketPatch,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ticket = await _load_ticket(db, ticket_id)
    before = _audit_snapshot(ticket)
    for field in payload.model_fields_set:
        setattr(ticket, field, getattr(payload, field))
    if ticket.status != before["status"]:
        ticket.position = None  # a status change outside the board rejoins the unplaced tail (spec §76.1)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="tickets", row_id=ticket.id,
        action="update", before=before, after=_audit_snapshot(ticket),
    )
    await db.commit()
    return await _ticket_json(db, await _load_ticket(db, ticket_id))


class TicketMove(BaseModel):
    """Drop a ticket into a board column at a place (spec §76)."""
    status: str
    index:  int = Field(ge=0, le=10_000)

    @field_validator("status")
    @classmethod
    def _board_status(cls, v):
        if v not in BOARD_STATUSES:
            raise ValueError(f"status must be one of {BOARD_STATUSES}; use Dismiss to hide a ticket")
        return v


@router.post("/api/tickets/{ticket_id}/move")
async def move_ticket(
    ticket_id: int, payload: TicketMove,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a ticket's status and its place in that column in one audited
    write. Renumbers the destination column. Last write wins."""
    ticket = await _load_ticket(db, ticket_id)
    if ticket.status == "dismissed":
        raise HTTPException(status_code=409, detail="Restore a dismissed ticket before moving it")
    before = {"status": ticket.status, "position": ticket.position}
    column = (await db.execute(select(Ticket).where(Ticket.status == payload.status))).scalars().all()
    ticket.status = payload.status
    positions = place(column, ticket, payload.index)
    for member in {*column, ticket}:
        member.position = positions[member.id]
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="tickets", row_id=ticket.id,
        action="update", before=before, after={"status": ticket.status, "position": ticket.position},
    )
    await db.commit()
    return {"ticket": await _ticket_json(db, await _load_ticket(db, ticket_id)), "positions": positions}


@router.delete("/api/tickets/{ticket_id}", status_code=204)
async def delete_ticket(
    ticket_id: int,
    tenant: Tenant = Depends(get_current_tenant), user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Permanent, with its votes and responses. Superadmin only."""
    ticket = await _load_ticket(db, ticket_id)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="tickets", row_id=ticket.id,
        action="delete", before={**_audit_snapshot(ticket), "response_count": len(ticket.responses)},
    )
    await db.delete(ticket)
    await db.commit()


class ResponseIn(BaseModel):
    body: str = Field(max_length=RESPONSE_MAX_CHARS)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v):
        if not v.strip():
            raise ValueError("A response cannot be empty")
        return v.strip()


@router.post("/api/tickets/{ticket_id}/responses", status_code=201)
async def add_response(
    ticket_id: int, payload: ResponseIn,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ticket = await _load_ticket(db, ticket_id)
    response = TicketResponse(ticket_id=ticket.id, author_user_id=user.id, body=payload.body)
    db.add(response)
    await db.flush()
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_responses", row_id=response.id,
        action="create", after={"ticket_id": ticket.id, "body": response.body},
    )
    await db.commit()
    ticket = await _load_ticket(db, ticket_id)
    created = next(r for r in ticket.responses if r.id == response.id)
    return {**response_dict(created), "author_user_id": created.author_user_id}


async def _own_or_superadmin_response(db: AsyncSession, ticket_id: int, response_id: int, user: User) -> TicketResponse:
    response = await db.get(TicketResponse, response_id)
    if response is None or response.ticket_id != ticket_id:
        raise HTTPException(status_code=404, detail="Response not found")
    if not user.is_superadmin and response.author_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the author or a superadmin can change a response")
    return response


@router.patch("/api/tickets/{ticket_id}/responses/{response_id}")
async def edit_response(
    ticket_id: int, response_id: int, payload: ResponseIn,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    response = await _own_or_superadmin_response(db, ticket_id, response_id, user)
    before = {"body": response.body}
    response.body = payload.body
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_responses", row_id=response.id,
        action="update", before=before, after={"body": response.body},
    )
    await db.commit()
    ticket = await _load_ticket(db, ticket_id)
    edited = next(r for r in ticket.responses if r.id == response_id)
    return {**response_dict(edited), "author_user_id": edited.author_user_id}


@router.delete("/api/tickets/{ticket_id}/responses/{response_id}", status_code=204)
async def delete_response(
    ticket_id: int, response_id: int,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    response = await _own_or_superadmin_response(db, ticket_id, response_id, user)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_responses", row_id=response.id,
        action="delete", before={"ticket_id": ticket_id, "body": response.body},
    )
    await db.delete(response)
    await db.commit()
