"""Schedule board (spec §82): one Discord message per chosen destination that
shows the next 7 days and is edited in place.

Everything on a board comes from `public_rows`, so a leadership-only event can
never appear. `run_board_refresh` is the per-minute entry point; it renders
each board, compares a hash with the stored one and only calls Discord when the
text changed. Discord is injected like in `event_engine`.
"""
import hashlib
import logging
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
MESSAGE_GONE = "MESSAGE_GONE"
SCOPES = ("kingdom", "alliance")

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MARKDOWN = re.compile(r"([\\*_~|>`\[\]])")


@dataclass(frozen=True)
class BoardLine:
    start: datetime
    name: str
    alliances: tuple[str, ...] = ()
    cancelled: bool = False


def escape_markdown(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text).replace("@", "@​")


def _line_text(line: BoardLine) -> str:
    clock = f"`{line.start:%H:%M}`"
    name = escape_markdown(line.name)
    stamp = f"<t:{int(line.start.timestamp())}:R>"
    if line.cancelled:
        return f"{clock} ~~{name}~~ cancelled"
    names = f" · {escape_markdown(', '.join(line.alliances))}" if line.alliances else ""
    return f"{clock} {name}{names} {stamp}"


def render_board(title: str, lines: list[BoardLine], today: date) -> str:
    """The message text. Pure. `lines` need not be sorted."""
    head = f"**{escape_markdown(title)}** · next {WINDOW_DAYS} days, times in UTC"
    if not lines:
        return f"{head}\n\nNothing is scheduled."
    out = [head]
    day: date | None = None
    shown = 0
    ordered = sorted(lines, key=lambda x: (x.start, x.name))
    for line in ordered:
        block = []
        if line.start.date() != day:
            day = line.start.date()
            marker = " (today)" if day == today else ""
            block.append(f"\n**{_DAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}**{marker}")
        block.append(_line_text(line))
        if len("\n".join(out + block)) > MAX_CHARS - 60:
            out.append(f"\n…and {len(ordered) - shown} more. The website has the full schedule.")
            return "\n".join(out)
        out.extend(block)
        shown += 1
    return "\n".join(out)


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
    merged: dict[int, dict] = {}
    for row in rows:
        when = ensure_utc(effective_start(row.occurrence))
        if not start <= when < end:
            continue
        entry = merged.setdefault(row.occurrence.id, {
            "start": when, "name": row.event.name, "names": [], "cancelled": row.occurrence.status == "cancelled"})
        if dest.board_scope != "alliance" and row.event.scope != "kingdom-wide" and row.tenant.name not in entry["names"]:
            entry["names"].append(row.tenant.name)
    lines = [BoardLine(e["start"], e["name"], tuple(sorted(e["names"])), e["cancelled"]) for e in merged.values()]
    return title, lines


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
        text = None
        error = None
        if scope is not None and not paused:
            try:
                title, lines = await board_lines(session, dest, now)
                text = render_board(title, lines, now.astimezone(timezone.utc).date())
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
    new_hash = content_hash(text)
    if message_id and new_hash == stored_hash and not had_error and not force:
        return "unchanged"
    if not token:
        await _store(session_factory, destination_id, board_error="No Discord bot token configured for this server",
                     board_refreshed_at=now)
        return "error"

    outcome = "edited"
    if message_id:
        ok, err = await discord.edit_channel_message(token, channel, message_id, text, no_mentions=True)
        if ok:
            await _store(session_factory, destination_id, board_hash=new_hash, board_error=None, board_refreshed_at=now)
            return "edited"
        if err != MESSAGE_GONE:
            await _store(session_factory, destination_id, board_error=err[:255], board_refreshed_at=now)
            return "error"
        outcome = "recreated"
    else:
        outcome = "posted"
    new_id, err = await discord.post_channel_message(token, channel, text, no_mentions=True)
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
