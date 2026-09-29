"""Public, unauthenticated surface for the community feedback board (spec
§40-43): submitting feedback/event-requests/announcement-requests/error
reports, listing the public board, anonymous upvoting, and serving the
standalone /feedback page — same trust level and same "no shared JS,
own inline <script>" pattern as routers/events.py's events.html (§22).

Kept as its own router (not folded into routers/events.py) because the
concern is genuinely different — a write-heavy, anonymous-submission
surface with its own data model, rather than another read of the
existing event/announcement schedule.
"""
import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Announcement, Occurrence, Tenant, Ticket, TicketVote
from services.sessions import SECRET_KEY

router = APIRouter()

_TITLE_MAX = 120
_DESCRIPTION_MAX = 2000  # same ceiling Announcement.body_markdown uses, spec §40

_KINDS = ("feedback", "event_request", "announcement_request", "error")
_FEEDBACK_CATEGORIES = ("Bug", "Suggestion", "Other")
_ERROR_TYPES = ("Wrong date or time", "Wrong channel", "Duplicate posting", "Didn't happen as scheduled", "Other")

# Tickets not yet declined show on the public board (spec §43.3) — 'open'
# is the natural default, and 'done'/'planned'/'in_progress' still stay
# visible so the community can see triage progress, not just the raw
# unsorted inbox.
_PUBLIC_STATUSES = ("open", "planned", "in_progress", "done")


def _hash_voter_id(raw_voter_id: str) -> str:
    """Spec §43.2 — never store the client-supplied X-Voter-Id verbatim.
    SECRET_KEY doubles as the server-side pepper here; it's already the
    one secret this deployment refuses to boot without (main.py), and a
    second, ticket-specific secret would be one more thing to provision
    for no real security gain over reusing this one for a different hash
    purpose."""
    return hashlib.sha256(f"{SECRET_KEY}:{raw_voter_id}".encode()).hexdigest()


class TicketIn(BaseModel):
    kind: str
    error_type: str | None = None
    title: str = Field(max_length=_TITLE_MAX)
    description: str = Field(max_length=_DESCRIPTION_MAX)
    tenant_slug: str | None = None
    related_occurrence_id: int | None = None
    related_announcement_id: int | None = None
    submitter_contact: str | None = None

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v):
        if v not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}")
        return v

    @field_validator("title", "description")
    @classmethod
    def _not_blank(cls, v):
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


def _ticket_public_dict(t: Ticket, tenant_by_id: dict, occ_by_id: dict, ann_by_id: dict, voted_ticket_ids: set) -> dict:
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
        "tenant_name": tenant.name if tenant else None,
        "tenant_slug": tenant.slug if tenant else None,
        "related_name": related_name,
        # Never submitter_contact — admin-only, see routers/admin/tickets.py.
        "voted_by_me": t.id in voted_ticket_ids,
    }


@router.get("/api/tickets")
async def list_tickets(x_voter_id: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    """Public board listing (spec §43.3) — every non-declined ticket,
    sorted by upvote_count descending. No submitter_contact anywhere in
    this response shape."""
    result = await db.execute(
        select(Ticket).where(Ticket.status.in_(_PUBLIC_STATUSES)).order_by(Ticket.upvote_count.desc(), Ticket.created_at.desc())
    )
    tickets = result.scalars().all()

    tenant_ids = {t.tenant_id for t in tickets if t.tenant_id}
    tenant_by_id = {}
    if tenant_ids:
        tres = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
        tenant_by_id = {t.id: t for t in tres.scalars().all()}

    occ_ids = {t.related_occurrence_id for t in tickets if t.related_occurrence_id}
    occ_by_id = {}
    if occ_ids:
        # Occurrence has no bare name column — go through its EventDefinition.
        from models.db import EventDefinition
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

    voter_hash = _hash_voter_id(x_voter_id) if x_voter_id else None
    voted_ticket_ids = set()
    if voter_hash and tickets:
        vres = await db.execute(
            select(TicketVote.ticket_id).where(
                TicketVote.voter_key == voter_hash, TicketVote.ticket_id.in_([t.id for t in tickets])
            )
        )
        voted_ticket_ids = {row[0] for row in vres.all()}

    return JSONResponse([_ticket_public_dict(t, tenant_by_id, occ_by_id, ann_by_id, voted_ticket_ids) for t in tickets])


@router.post("/api/tickets")
async def create_ticket(payload: TicketIn, db: AsyncSession = Depends(get_db)):
    tenant_id = None
    if payload.tenant_slug:
        result = await db.execute(select(Tenant).where(Tenant.slug == payload.tenant_slug))
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=404, detail=f"No such alliance: {payload.tenant_slug}")
        tenant_id = tenant.id

    if payload.related_occurrence_id and payload.related_announcement_id:
        raise HTTPException(status_code=422, detail="A ticket can reference an occurrence or an announcement, not both")

    if payload.kind == "error" and not payload.related_occurrence_id and not payload.related_announcement_id:
        raise HTTPException(status_code=422, detail="An error report must reference the event or announcement it's about")

    if payload.error_type is not None:
        valid_types = _FEEDBACK_CATEGORIES if payload.kind == "feedback" else _ERROR_TYPES
        if payload.kind in ("feedback", "error") and payload.error_type not in valid_types:
            raise HTTPException(status_code=422, detail=f"error_type must be one of {valid_types}")

    ticket = Ticket(
        kind=payload.kind,
        error_type=payload.error_type,
        title=payload.title,
        description=payload.description,
        tenant_id=tenant_id,
        related_occurrence_id=payload.related_occurrence_id,
        related_announcement_id=payload.related_announcement_id,
        submitter_contact=(payload.submitter_contact or "").strip() or None,
        status="open",
        upvote_count=1,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)

    return JSONResponse(
        {
            "id": ticket.id,
            "kind": ticket.kind,
            "title": ticket.title,
            "status": ticket.status,
            "upvote_count": ticket.upvote_count,
        },
        status_code=201,
    )


@router.post("/api/tickets/{ticket_id}/vote")
async def vote_ticket(ticket_id: int, x_voter_id: str = Header(default=""), db: AsyncSession = Depends(get_db)):
    """Spec §43.2 — a toggle, not an accumulator: voting again for the
    same (ticket, voter) pair removes the earlier vote instead of adding
    a second one."""
    if not x_voter_id:
        raise HTTPException(status_code=400, detail="X-Voter-Id header is required")

    ticket = await db.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    voter_hash = _hash_voter_id(x_voter_id)
    existing = await db.execute(
        select(TicketVote).where(TicketVote.ticket_id == ticket_id, TicketVote.voter_key == voter_hash)
    )
    vote = existing.scalar_one_or_none()

    if vote:
        await db.delete(vote)
        ticket.upvote_count = max(0, ticket.upvote_count - 1)
        voted = False
    else:
        db.add(TicketVote(ticket_id=ticket_id, voter_key=voter_hash))
        ticket.upvote_count += 1
        voted = True

    await db.commit()
    return JSONResponse({"id": ticket.id, "upvote_count": ticket.upvote_count, "voted_by_me": voted})


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page():
    with open("/app/static/feedback.html") as f:
        return HTMLResponse(f.read())
