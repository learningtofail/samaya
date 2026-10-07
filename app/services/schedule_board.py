"""Schedule board (spec §82): one Discord message per chosen destination that
shows the next 7 days and is edited in place.

Everything on a board comes from `public_rows`, so a leadership-only event can
never appear. `run_board_refresh` is the per-minute entry point; it renders
each board, compares a hash with the stored one and only calls Discord when the
text changed. Discord is injected like in `event_engine`.
"""
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import AudienceDestination, Kingdom, Tenant
from services.event_engine import effective_start, platform_bot_token
from services.public_events import public_rows
from services.time_utils import ensure_utc

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
MAX_CHARS = 1900
BATCH_SIZE = 20
ERROR_RETRY_AFTER = timedelta(minutes=10)
HEARTBEAT_AFTER = timedelta(minutes=15)  # spec §82.9: refresh the Last edit line even when nothing changed
DEFAULT_PUBLIC_BASE_URL = "https://ks138.taraka.dev"
MESSAGE_GONE = "MESSAGE_GONE"
SCOPES = ("kingdom", "alliance")
KINGDOM_GROUP = "Kingdom-wide"  # spec §82.9 item 5: the first section of a board

_BOARD_BUTTON = re.compile(r"(bn|bi):([0-9]{1,9})")


def board_buttons(destination_id: int) -> list[dict]:
    """Spec §82.10: the two buttons under a board. Each opens a private menu."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 1, "label": "Notify me", "custom_id": f"bn:{destination_id}"},
        {"type": 2, "style": 3, "label": "I'm in", "custom_id": f"bi:{destination_id}"}]}]


def parse_board_button(value) -> tuple[str, int] | None:
    """("bn" or "bi", destination id), or None."""
    match = _BOARD_BUTTON.fullmatch(value) if isinstance(value, str) else None
    return (match.group(1), int(match.group(2))) if match else None


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MARKDOWN = re.compile(r"([\\*_~|>`\[\]])")


@dataclass(frozen=True)
class BoardLine:
    start: datetime
    name: str
    alliances: tuple[str, ...] = ()
    cancelled: bool = False
    going: int = 0  # spec §87.10: the "I'm in" count, shown only above zero
    duration_hours: float | None = None  # spec §82.9: shown when the event has one
    group: str = ""  # spec §82.9 item 5: section heading ("Kingdom-wide" or an alliance name); empty means no sections
    event_id: int | None = None  # spec §82.10: what the private menus offer; never rendered
    day: date | None = None
    signup: bool = False
    rsvp: bool = False


def escape_markdown(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text).replace("@", "@​")


def public_base_url() -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE_URL).rstrip("/")


def format_duration(hours: float | None) -> str:
    """`2 h`, `1.5 h` or `45 min`; empty for an event with no duration."""
    if not hours or hours <= 0:
        return ""
    minutes = round(hours * 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h" if minutes % 60 == 0 else f"{minutes / 60:g} h"


def _line_text(line: BoardLine) -> str:
    clock = f"`{line.start:%H:%M}`"
    name = escape_markdown(line.name)
    if line.cancelled:
        return f"{clock} ~~{name}~~ cancelled"
    stamp = int(line.start.timestamp())
    parts = [
        escape_markdown(", ".join(line.alliances)) if line.alliances and not line.group else "",
        f"{line.going} in" if line.going > 0 else "",
        format_duration(line.duration_hours),
        f"<t:{stamp}:t> your time",
        f"<t:{stamp}:R>",
    ]
    return f"{clock} **{name}** · " + " · ".join(p for p in parts if p)


def _footer(links: list[tuple[str, str]]) -> str:
    if not links:
        return ""
    return "\n\n🔗 " + " · ".join(f"[{escape_markdown(label)}](<{url}>)" for label, url in links)


def render_board(title: str, lines: list[BoardLine], today: date, links: list[tuple[str, str]] | None = None,
                 updated: datetime | None = None) -> str:
    """The message text. Pure. `lines` need not be sorted. `updated` adds the Last edit line (spec §82.9);
    the hash that decides whether to edit is taken without it."""
    head = f"📅 **{escape_markdown(title)}**"
    if updated is not None:
        stamp = int(updated.timestamp())
        head += f"\nLast edit: <t:{stamp}:f> (<t:{stamp}:R>)"
    head += f"\nNext {WINDOW_DAYS} days · times in UTC"
    footer = _footer(links or [])
    if not lines:
        return f"{head}\n\nNothing is scheduled.{footer}"
    out = [head]
    shown = 0
    total = len(lines)
    budget = MAX_CHARS - len(footer) - 60
    groups = sorted({x.group for x in lines}, key=lambda g: (0 if g == "" else 1 if g == KINGDOM_GROUP else 2, g.lower()))
    for group in groups:
        day: date | None = None
        first = True
        for line in sorted((x for x in lines if x.group == group), key=lambda x: (x.start, x.name)):
            block = []
            if first and group:
                block.append(f"\n### {escape_markdown(group)}")
            if line.start.date() != day:
                day = line.start.date()
                marker = " (today)" if day == today else ""
                block.append(f"\n**{_DAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}**{marker}")
            block.append(_line_text(line))
            if len("\n".join(out + block)) > budget:
                out.append(f"\n…and {total - shown} more. The website has the full schedule.")
                return "\n".join(out) + footer
            out.extend(block)
            shown += 1
            first = False
    return "\n".join(out) + footer


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def board_lines(db: AsyncSession, dest: AudienceDestination, now: datetime) -> tuple[str, list[BoardLine]]:
    """(title, lines) for one destination's board. Raises ValueError when the scope cannot be resolved."""
    today = now.astimezone(timezone.utc).date()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    end = start + timedelta(days=WINDOW_DAYS)
    wide = (today - timedelta(days=1), today + timedelta(days=WINDOW_DAYS))  # a moved occurrence can change date
    if dest.board_scope == "alliance":
        tenant = await db.get(Tenant, dest.board_tenant_id) if dest.board_tenant_id else None
        if tenant is None:
            raise ValueError("The alliance for this board was removed. Choose another scope.")
        rows = await public_rows(db, *wide, tenant=tenant)
        title = f"{tenant.name} schedule"
    else:
        kingdom = await db.get(Kingdom, dest.audience.kingdom_id)
        rows = [r for r in await public_rows(db, *wide, tenant=None) if r.tenant.kingdom_id == kingdom.id]
        title = f"{kingdom.name} schedule"
    merged: dict[tuple[int, str], dict] = {}
    for row in rows:
        when = ensure_utc(effective_start(row.occurrence))
        if not start <= when < end:
            continue
        group = KINGDOM_GROUP if row.event.scope == "kingdom-wide" else row.tenant.name
        merged.setdefault((row.occurrence.id, group), {
            "start": when, "name": row.event.name, "group": group, "cancelled": row.occurrence.status == "cancelled",
            "key": (row.event.id, row.occurrence.occurrence_date), "rsvp": row.event.rsvp_enabled,
            "signup": row.event.signup_enabled, "duration": row.event.duration_hours})
    from services.signup import rsvp_counts  # imported here: signup imports event_engine, as this module does
    going = await rsvp_counts(db, [e["key"] for e in merged.values() if e["rsvp"] and not e["cancelled"]])
    lines = [BoardLine(e["start"], e["name"], (), e["cancelled"], going.get(e["key"], 0), e["duration"], e["group"],
                       e["key"][0], e["key"][1], e["signup"], e["rsvp"])
             for e in merged.values()]
    return title, lines


async def board_links(db: AsyncSession, dest: AudienceDestination, lines: list[BoardLine]) -> list[tuple[str, str]]:
    """Public page links for the footer (spec §82.9): an alliance board links its own page; a Kingdom board links
    the combined page and each alliance that has an event in the window."""
    base = public_base_url()
    if dest.board_scope == "alliance":
        tenant = await db.get(Tenant, dest.board_tenant_id) if dest.board_tenant_id else None
        return [(f"{tenant.name} schedule", f"{base}/events/{tenant.slug}")] if tenant else []
    links = [("Full schedule", f"{base}/events")]
    names = {line.group for line in lines if line.group and line.group != KINGDOM_GROUP}
    if names:
        kingdom_id = dest.audience.kingdom_id
        tenants = (await db.execute(select(Tenant).where(Tenant.kingdom_id == kingdom_id, Tenant.name.in_(names))
                                    .order_by(Tenant.name))).scalars().all()
        links += [(t.name, f"{base}/events/{t.slug}") for t in tenants]
    return links


async def _store(session_factory, destination_id: int, **values) -> None:
    async with session_factory() as session:
        dest = await session.get(AudienceDestination, destination_id)
        if dest is None:
            return
        for key, value in values.items():
            setattr(dest, key, value)
        await session.commit()


async def refresh_destination(session_factory, discord, destination_id: int, now: datetime, force: bool = False) -> str:
    """Brings one destination's board in line with its setting. Returns what happened:
    off, skipped, unchanged, posted, edited, recreated, removed or error. Never raises."""
    try:
        return await _refresh(session_factory, discord, destination_id, now, force)
    except Exception as exc:
        logger.exception("board %s failed", destination_id)
        await _store(session_factory, destination_id, board_error=f"{type(exc).__name__}: {exc}"[:255], board_refreshed_at=now)
        return "error"


async def _refresh(session_factory, discord, destination_id: int, now: datetime, force: bool) -> str:
    async with session_factory() as session:
        dest = await session.get(AudienceDestination, destination_id)
        if dest is None:
            return "off"
        scope, message_id, stored_hash = dest.board_scope, dest.board_message_id, dest.board_hash
        channel = dest.channel_id
        token = dest.server.bot_token or platform_bot_token()
        paused = dest.paused_at is not None
        last = ensure_utc(dest.board_refreshed_at) if dest.board_refreshed_at else None
        had_error = bool(dest.board_error)
        text = body = None
        error = None
        if scope is not None and not paused:
            try:
                title, lines = await board_lines(session, dest, now)
                links = await board_links(session, dest, lines)
                today = now.astimezone(timezone.utc).date()
                body = render_board(title, lines, today, links)
                text = render_board(title, lines, today, links, updated=now)
            except ValueError as exc:
                error = str(exc)

    if scope is None:
        if not message_id:
            return "off"
        if not token:
            await _store(session_factory, destination_id, board_error="No Discord bot token configured for this server")
            return "error"
        ok, err = await discord.delete_channel_message(token, channel, message_id)
        if not ok:
            await _store(session_factory, destination_id, board_error=f"Could not remove the board message: {err}"[:255],
                         board_refreshed_at=now)
            return "error"
        await _store(session_factory, destination_id, board_message_id=None, board_hash=None, board_error=None,
                     board_refreshed_at=now)
        return "removed"
    if paused:
        return "skipped"
    if error:
        await _store(session_factory, destination_id, board_error=error[:255], board_refreshed_at=now)
        return "error"
    if had_error and last is not None and now - last < ERROR_RETRY_AFTER and not force:
        return "skipped"
    new_hash = content_hash(body)
    if message_id and new_hash == stored_hash and not had_error and not force:
        if last is None or now - last < HEARTBEAT_AFTER:
            return "unchanged"
    if not token:
        await _store(session_factory, destination_id, board_error="No Discord bot token configured for this server",
                     board_refreshed_at=now)
        return "error"

    outcome = "edited"
    if message_id:
        ok, err = await discord.edit_channel_message(token, channel, message_id, text, no_mentions=True,
                                                    components=board_buttons(destination_id))
        if ok:
            await _store(session_factory, destination_id, board_hash=new_hash, board_error=None, board_refreshed_at=now)
            return "edited"
        if err != MESSAGE_GONE:
            await _store(session_factory, destination_id, board_error=err[:255], board_refreshed_at=now)
            return "error"
        outcome = "recreated"
    else:
        outcome = "posted"
    new_id, err = await discord.post_channel_message(token, channel, text, no_mentions=True,
                                                      components=board_buttons(destination_id))
    if not new_id:
        await _store(session_factory, destination_id, board_message_id=None, board_hash=None, board_error=err[:255],
                     board_refreshed_at=now)
        return "error"
    await _store(session_factory, destination_id, board_message_id=new_id, board_hash=new_hash, board_error=None,
                 board_refreshed_at=now)
    return outcome


async def run_board_refresh(session_factory, discord, now: datetime | None = None) -> dict[str, int]:
    """The per-minute pass over every destination with a board or a leftover board message."""
    now = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        ids = (await session.execute(
            select(AudienceDestination.id)
            .where(or_(AudienceDestination.board_scope.is_not(None), AudienceDestination.board_message_id.is_not(None)))
            .order_by(AudienceDestination.board_refreshed_at.asc().nulls_first(), AudienceDestination.id)
            .limit(BATCH_SIZE)
        )).scalars().all()
    counts: dict[str, int] = {}
    for destination_id in ids:
        outcome = await refresh_destination(session_factory, discord, destination_id, now)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
