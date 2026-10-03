"""Shared shaping of tickets and their public responses (spec §66.10) for the
public board and the admin Feedback tab, so both read the same author name
and the same status groups."""
from models.db import Event, EventOccurrence, Ticket, TicketChecklistItem, TicketResponse, TicketTag
from services.contrast import pick_ink
from sqlalchemy import select

ACTIVE_STATUSES = ("open", "planned", "in_progress")
ARCHIVED_STATUSES = ("done", "declined")
ALL_STATUSES = ACTIVE_STATUSES + ARCHIVED_STATUSES + ("dismissed",)
RESPONSE_MAX_CHARS = 2000
ANONYMOUS_AUTHOR = "Team"


def author_name(response: TicketResponse) -> str:
    """The public author name is the user's display name, resolved now so a
    rename updates earlier replies. Never the Discord username."""
    author = response.author
    return (author.display_name or "").strip() or ANONYMOUS_AUTHOR if author else ANONYMOUS_AUTHOR


def response_dict(response: TicketResponse) -> dict:
    return {
        "id":         response.id,
        "author":     author_name(response),
        "body":       response.body,
        "created_at": response.created_at.isoformat() if response.created_at else None,
        "updated_at": response.updated_at.isoformat() if response.updated_at else None,
    }


def checklist_dict(item: TicketChecklistItem) -> dict:
    """Admin only (spec §76.5): never part of a public payload."""
    return {"id": item.id, "body": item.body, "done": bool(item.done), "position": item.position}


def tag_dict(tag: TicketTag) -> dict:
    """Admin only (spec §76.5). `ink` is the readable label text color on the tag color."""
    return {"id": tag.id, "name": tag.name, "color": tag.color, "ink": pick_ink(tag.color)}


async def occurrence_names(db, tickets: list[Ticket]) -> dict[int, str]:
    ids = {t.related_occurrence_id for t in tickets if t.related_occurrence_id}
    if not ids:
        return {}
    rows = await db.execute(
        select(EventOccurrence.id, Event.name)
        .join(Event, Event.id == EventOccurrence.event_id).where(EventOccurrence.id.in_(ids))
    )
    return {occ_id: name for occ_id, name in rows.all()}
