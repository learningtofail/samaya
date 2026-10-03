"""Ordering and placing tickets on the admin board (spec §76).

Pure functions over Ticket-like objects (anything with id, position,
upvote_count and created_at), so they are tested without a database.
"""
from typing import Iterable

from services.time_utils import ensure_utc

# The five board columns are the statuses a ticket can sit in publicly.
# "dismissed" is deliberately not a column (spec §76.1).
BOARD_STATUSES = ("open", "planned", "in_progress", "done", "declined")


def _sort_key(ticket) -> tuple:
    created = ensure_utc(ticket.created_at).timestamp() if ticket.created_at else 0.0
    return (ticket.position is None, ticket.position or 0, -ticket.upvote_count, -created, -ticket.id)


def ordered(tickets: Iterable) -> list:
    """Placed tickets first by position, then the rest by votes and recency."""
    return sorted(tickets, key=_sort_key)


def place(column: Iterable, moved, index: int) -> dict[int, int]:
    """New positions for a destination column after `moved` lands at `index`.

    `index` counts the column without the moved ticket and is clamped to its
    length, so a too-large value means "at the end". The whole column is
    renumbered 0, 1, 2, ... so positions never need rebalancing.
    """
    others = [t for t in ordered(column) if t.id != moved.id]
    others.insert(max(0, min(index, len(others))), moved)
    return {t.id: n for n, t in enumerate(others)}
