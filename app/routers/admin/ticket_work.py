"""Internal work on tickets (spec §76.5): checklist, tags and export.

Admin only. Nothing here reaches a public endpoint. Every mutating route
returns the whole updated admin ticket, so the board can replace it in place.
Viewers read and export but change nothing (§31.3).
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import get_db
from models.db import Tenant, Ticket, TicketChecklistItem, TicketTag, TicketTagLink, User
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.ticket_export import export_json, export_markdown_zip, ticket_export_dict
from services.ticket_views import occurrence_names, tag_dict
from services.validators import parse_hex_color

from .deps import get_current_tenant, get_current_user, require_not_viewer
from .tickets import _load_ticket, _ticket_json

router = APIRouter()

MAX_CHECKLIST_ITEMS = 50
MAX_TAGS_PER_TICKET = 8
_TAG_NAME_MAX = 24
_CHECKLIST_MAX = 200


def _clean(v: str, what: str) -> str:
    v = " ".join(v.split())
    if not v:
        raise ValueError(f"{what} must not be blank")
    return v


# ── Checklist ────────────────────────────────────────────────────────────────

class ChecklistIn(BaseModel):
    body: str = Field(max_length=_CHECKLIST_MAX)

    @field_validator("body")
    @classmethod
    def _body(cls, v):
        return _clean(v, "A checklist item")


class ChecklistPatch(BaseModel):
    body: Optional[str] = Field(default=None, max_length=_CHECKLIST_MAX)
    done: Optional[bool] = None

    @field_validator("body")
    @classmethod
    def _body(cls, v):
        return _clean(v, "A checklist item") if v is not None else v


async def _item(db: AsyncSession, ticket_id: int, item_id: int) -> TicketChecklistItem:
    item = await db.get(TicketChecklistItem, item_id)
    if item is None or item.ticket_id != ticket_id:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    return item


@router.post("/api/tickets/{ticket_id}/checklist", status_code=201)
async def add_checklist_item(
    ticket_id: int, payload: ChecklistIn,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ticket = await _load_ticket(db, ticket_id)
    if len(ticket.checklist) >= MAX_CHECKLIST_ITEMS:
        raise HTTPException(status_code=422, detail=f"A ticket can have at most {MAX_CHECKLIST_ITEMS} checklist items")
    position = max((i.position for i in ticket.checklist), default=-1) + 1
    item = TicketChecklistItem(ticket_id=ticket.id, body=payload.body, done=False, position=position)
    db.add(item)
    await db.flush()
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_checklist_items", row_id=item.id,
        action="create", after={"ticket_id": ticket.id, "body": item.body},
    )
    await db.commit()
    return await _ticket_json(db, await _load_ticket(db, ticket_id))


@router.patch("/api/tickets/{ticket_id}/checklist/{item_id}")
async def update_checklist_item(
    ticket_id: int, item_id: int, payload: ChecklistPatch,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await _item(db, ticket_id, item_id)
    before = {"body": item.body, "done": bool(item.done)}
    for field in payload.model_fields_set:
        if getattr(payload, field) is not None:
            setattr(item, field, getattr(payload, field))
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_checklist_items", row_id=item.id,
        action="update", before=before, after={"body": item.body, "done": bool(item.done)},
    )
    await db.commit()
    return await _ticket_json(db, await _load_ticket(db, ticket_id))


@router.delete("/api/tickets/{ticket_id}/checklist/{item_id}")
async def delete_checklist_item(
    ticket_id: int, item_id: int,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await _item(db, ticket_id, item_id)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_checklist_items", row_id=item.id,
        action="delete", before={"ticket_id": ticket_id, "body": item.body, "done": bool(item.done)},
    )
    await db.delete(item)
    await db.commit()
    return await _ticket_json(db, await _load_ticket(db, ticket_id))


# ── Tags ─────────────────────────────────────────────────────────────────────

_TAG_TAKEN = {"uq_ticket_tag_name": "A tag with that name already exists", "ticket_tags_name_key": "A tag with that name already exists"}


class TagIn(BaseModel):
    name: str = Field(max_length=_TAG_NAME_MAX)
    color: str

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _clean(v, "A tag name")

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return parse_hex_color(v)


class TagPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=_TAG_NAME_MAX)
    color: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _clean(v, "A tag name") if v is not None else v

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return parse_hex_color(v) if v is not None else v


class TicketTagsIn(BaseModel):
    tag_ids: list[int] = Field(max_length=MAX_TAGS_PER_TICKET * 4)


async def _reject_name_clash(db: AsyncSession, name: str, except_id: int | None = None) -> None:
    """Tag names are unique ignoring case. The column constraint only catches an exact match."""
    stmt = select(TicketTag.id).where(func.lower(TicketTag.name) == name.lower())
    if except_id is not None:
        stmt = stmt.where(TicketTag.id != except_id)
    if (await db.execute(stmt)).first():
        raise HTTPException(status_code=422, detail="A tag with that name already exists")


async def _tag(db: AsyncSession, tag_id: int) -> TicketTag:
    tag = await db.get(TicketTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return tag


async def _tag_usage(db: AsyncSession, tag_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(TicketTagLink).where(TicketTagLink.tag_id == tag_id))).scalar_one()


@router.get("/api/ticket-tags")
async def list_ticket_tags(tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    tags = (await db.execute(select(TicketTag).order_by(func.lower(TicketTag.name)))).scalars().all()
    counts = dict((await db.execute(
        select(TicketTagLink.tag_id, func.count()).group_by(TicketTagLink.tag_id))).all())
    return [{**tag_dict(t), "ticket_count": counts.get(t.id, 0)} for t in tags]


@router.post("/api/ticket-tags", status_code=201)
async def create_ticket_tag(
    payload: TagIn, tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _reject_name_clash(db, payload.name)
    tag = TicketTag(name=payload.name, color=payload.color)
    db.add(tag)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _TAG_TAKEN, fallback="Could not create the tag")
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_tags", row_id=tag.id,
        action="create", after={"name": tag.name, "color": tag.color},
    )
    await db.commit()
    return {**tag_dict(tag), "ticket_count": 0}


@router.patch("/api/ticket-tags/{tag_id}")
async def update_ticket_tag(
    tag_id: int, payload: TagPatch,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tag = await _tag(db, tag_id)
    before = {"name": tag.name, "color": tag.color}
    if payload.name is not None and payload.name.lower() != tag.name.lower():
        await _reject_name_clash(db, payload.name, except_id=tag.id)
    for field in payload.model_fields_set:
        if getattr(payload, field) is not None:
            setattr(tag, field, getattr(payload, field))
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _TAG_TAKEN, fallback="Could not update the tag")
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_tags", row_id=tag.id,
        action="update", before=before, after={"name": tag.name, "color": tag.color},
    )
    await db.commit()
    return {**tag_dict(tag), "ticket_count": await _tag_usage(db, tag.id)}


@router.delete("/api/ticket-tags/{tag_id}", status_code=204)
async def delete_ticket_tag(
    tag_id: int, tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Removes the tag from every ticket that carries it."""
    tag = await _tag(db, tag_id)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_tags", row_id=tag.id,
        action="delete", before={"name": tag.name, "color": tag.color, "ticket_count": await _tag_usage(db, tag.id)},
    )
    await db.delete(tag)
    await db.commit()


@router.put("/api/tickets/{ticket_id}/tags")
async def set_ticket_tags(
    ticket_id: int, payload: TicketTagsIn,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the ticket's whole tag set. Repeated ids count once."""
    ticket = await _load_ticket(db, ticket_id)
    wanted = list(dict.fromkeys(payload.tag_ids))
    if len(wanted) > MAX_TAGS_PER_TICKET:
        raise HTTPException(status_code=422, detail=f"A ticket can have at most {MAX_TAGS_PER_TICKET} tags")
    found = (await db.execute(select(TicketTag).where(TicketTag.id.in_(wanted)))).scalars().all() if wanted else []
    missing = sorted(set(wanted) - {t.id for t in found})
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown tag id(s): {missing}")
    before = [t.name for t in ticket.tags]
    ticket.tags = sorted(found, key=lambda t: t.name.lower())
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="tickets", row_id=ticket.id,
        action="update", before={"tags": before}, after={"tags": [t.name for t in ticket.tags]},
    )
    await db.commit()
    return await _ticket_json(db, await _load_ticket(db, ticket_id))


# ── Export ───────────────────────────────────────────────────────────────────

@router.get("/api/tickets/export")
async def export_tickets(
    fmt: str = Query("json", alias="format", pattern="^(json|markdown)$"),
    include_contact: bool = False,
    tenant: Tenant = Depends(get_current_tenant), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every ticket, dismissed ones included, as one JSON file or a zip of Markdown files.
    The submitter's private contact is only included when asked for, and the export is audited."""
    tickets = (await db.execute(
        select(Ticket).options(selectinload(Ticket.responses), selectinload(Ticket.checklist), selectinload(Ticket.tags))
        .order_by(Ticket.id)
    )).scalars().unique().all()
    tenants = {t.id: t.name for t in (await db.execute(select(Tenant))).scalars().all()}
    related = await occurrence_names(db, list(tickets))
    rows = [
        ticket_export_dict(
            t, include_contact=include_contact,
            tenant_name=tenants.get(t.tenant_id), related_name=related.get(t.related_occurrence_id),
        ) for t in tickets
    ]
    now = datetime.now(timezone.utc)
    # The audit table only allows create/update/delete (ck_audit_action), so an
    # export is recorded as a "create" on the pseudo-table "ticket_exports".
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="ticket_exports", row_id=None, action="create",
        after={"format": fmt, "include_contact": include_contact, "ticket_count": len(rows)},
    )
    await db.commit()
    stamp = now.strftime("%Y%m%d")
    if fmt == "markdown":
        content, media, name = export_markdown_zip(rows), "application/zip", f"samaya-tickets-{stamp}.zip"
    else:
        content, media, name = export_json(rows, now), "application/json", f"samaya-tickets-{stamp}.json"
    return Response(content=content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
