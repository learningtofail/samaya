"""Public, unauthenticated surface for the community feedback board (spec
§40-43): submitting feedback/event-requests/announcement-requests/error
reports, listing the public board, anonymous upvoting, and serving the
standalone /feedback page — same trust level as routers/events.py's
events.html, and (since Phase 3 audit remediation moved feedback.html's
former inline <style>/<script> into standalone feedback.css/feedback.js)
the same "own external static assets, no shared JS/CSS with admin.html or
events.html" pattern too (§22).

Kept as its own router (not folded into routers/events.py) because the
concern is genuinely different — a write-heavy, anonymous-submission
surface with its own data model, rather than another read of the
existing event/announcement schedule.
"""
import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import selectinload

from models import get_db
from models.db import EventOccurrence, Tenant, Ticket, TicketVote
from services.rate_limit import RateLimiter
from services.sessions import SECRET_KEY
from services.public_pages import get_public_kingdom, get_request_theme, mark_preview, render_public_page
from services.ticket_views import ACTIVE_STATUSES, ARCHIVED_STATUSES, occurrence_names, response_dict

router = APIRouter()

# Audit remediation, Phase 5 — see services/rate_limit.py's own docstring.
# The write endpoints get the tight limits (this board has no CAPTCHA or
# any other anti-spam layer); the listing gets a generous one mainly to
# cap a runaway client/script rather than to constrain a real visitor —
# feedback.js only ever fetches it once per page load, no polling.
_create_ticket_limit = RateLimiter("create_ticket", max_requests=5, window_seconds=600)
_vote_ticket_limit = RateLimiter("vote_ticket", max_requests=30, window_seconds=600)
_list_tickets_limit = RateLimiter("list_tickets", max_requests=60, window_seconds=60)

_TITLE_MAX = 120
_DESCRIPTION_MAX = 2000  # same ceiling Announcement.body_markdown uses, spec §40

_KINDS = ("feedback", "event_request", "announcement_request", "error")
_FEEDBACK_CATEGORIES = ("Bug", "Suggestion", "Other")
_ERROR_TYPES = ("Wrong date or time", "Wrong channel", "Duplicate posting", "Didn't happen as scheduled", "Other")

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


def _ticket_public_dict(t: Ticket, tenant_by_id: dict, occ_names: dict, voted_ticket_ids: set) -> dict:
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
        "related_name": occ_names.get(t.related_occurrence_id),
        "responses": [response_dict(r) for r in t.responses],
        # Never submitter_contact — admin-only, see routers/admin/tickets.py.
        "voted_by_me": t.id in voted_ticket_ids,
    }


@router.get("/api/tickets")
async def list_tickets(
    x_voter_id: str = Header(default=""), db: AsyncSession = Depends(get_db), _rl: None = Depends(_list_tickets_limit)
):
    """Public board (spec §66.10): `active` (open, planned, in progress,
    most upvoted first) and `archived` (done and declined, newest first,
    read-only). Dismissed tickets are in neither. No submitter_contact."""
    result = await db.execute(
        select(Ticket).options(selectinload(Ticket.responses))
        .where(Ticket.status.in_(ACTIVE_STATUSES + ARCHIVED_STATUSES))
        .order_by(Ticket.upvote_count.desc(), Ticket.created_at.desc(), Ticket.id.desc())
    )
    tickets = result.scalars().unique().all()

    tenant_ids = {t.tenant_id for t in tickets if t.tenant_id}
    tenant_by_id = {}
    if tenant_ids:
        tres = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
        tenant_by_id = {t.id: t for t in tres.scalars().unique().all()}
    occ_names = await occurrence_names(db, tickets)

    voted_ticket_ids = set()
    if x_voter_id and tickets:
        vres = await db.execute(
            select(TicketVote.ticket_id).where(
                TicketVote.voter_key == _hash_voter_id(x_voter_id), TicketVote.ticket_id.in_([t.id for t in tickets])
            )
        )
        voted_ticket_ids = {row[0] for row in vres.all()}

    rows = [_ticket_public_dict(t, tenant_by_id, occ_names, voted_ticket_ids) for t in tickets]
    archived = sorted((r for r in rows if r["status"] in ARCHIVED_STATUSES), key=lambda r: r["created_at"] or "", reverse=True)
    return JSONResponse({"active": [r for r in rows if r["status"] in ACTIVE_STATUSES], "archived": archived})


@router.post("/api/tickets")
async def create_ticket(payload: TicketIn, db: AsyncSession = Depends(get_db), _rl: None = Depends(_create_ticket_limit)):
    tenant_id = None
    if payload.tenant_slug:
        result = await db.execute(select(Tenant).where(Tenant.slug == payload.tenant_slug))
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=404, detail=f"No such alliance: {payload.tenant_slug}")
        tenant_id = tenant.id

    if payload.kind == "error" and not payload.related_occurrence_id:
        raise HTTPException(status_code=422, detail="An error report must reference the event it's about")
    if payload.related_occurrence_id and await db.get(EventOccurrence, payload.related_occurrence_id) is None:
        raise HTTPException(status_code=422, detail="That event no longer exists")

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
async def vote_ticket(
    ticket_id: int,
    x_voter_id: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(_vote_ticket_limit),
):
    """Spec §43.2 — a toggle, not an accumulator: voting again for the
    same (ticket, voter) pair removes the earlier vote instead of adding
    a second one."""
    if not x_voter_id:
        raise HTTPException(status_code=400, detail="X-Voter-Id header is required")

    ticket = await db.get(Ticket, ticket_id)
    if not ticket or ticket.status == "dismissed":
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket.status in ARCHIVED_STATUSES:
        raise HTTPException(status_code=409, detail="This ticket is archived and can no longer be voted on")

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
async def feedback_page(request: Request, db: AsyncSession = Depends(get_db)):
    # render_public_page() rewrites /static/... references with
    # ?v=STATIC_ASSET_VERSION (Cloudflare edge-caches /static/*.css/*.js for
    # hours regardless of origin freshness, spec §23) and serves the page in
    # the visitor's language (spec §72).
    kingdom = await get_public_kingdom(db)
    theme, preview = await get_request_theme(request, db, kingdom)
    response = render_public_page(
        request, "feedback.html", ("public.common.", "public.feedback."), "public.feedback.title", kingdom, theme)
    return mark_preview(response) if preview else response
