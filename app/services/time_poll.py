"""Time polls (spec §84): a leader proposes 2 to 6 UTC slots, players tap buttons
in Discord, Samaya tallies, the leader picks the winner. Nothing is applied here.

Votes are stored before any Discord call. A voter is stored as an HMAC of the
poll and the Discord user, never as the Discord ID. Discord is injected like in
`event_engine`, so tests use `FakeDiscord`.
"""
import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import DiscordServer, TimePoll, TimePollMessage, TimePollSlot, TimePollVote
from services.event_engine import platform_bot_token
from services.rate_limit import RateLimiter
from services.schedule_board import MESSAGE_GONE, content_hash, escape_markdown
from services.time_utils import ensure_utc

logger = logging.getLogger(__name__)

MIN_SLOTS = 2
MAX_SLOTS = 6
MAX_OPEN_PER_ALLIANCE = 3
DEFAULT_CLOSE_HOURS = 48
MAX_CLOSE_HOURS = 14 * 24
RETENTION_DAYS = 90
EDIT_THROTTLE = timedelta(minutes=2)
ERROR_RETRY_AFTER = timedelta(minutes=10)
FINAL_EDIT_WINDOW = timedelta(days=7)
BATCH_SIZE = 20
BUTTONS_PER_ROW = 5
BAR_WIDTH = 10

CALLBACK_CHANNEL_MESSAGE = 4
CALLBACK_UPDATE_MESSAGE = 7
EPHEMERAL = 64
NO_MENTIONS = {"parse": []}

_CUSTOM_ID = re.compile(r"tp:([0-9]{1,9}):([0-9]{1,9})")
_click_limit = RateLimiter("time_poll_click", max_requests=10, window_seconds=60)


# ── Pure helpers ─────────────────────────────────────────────

def custom_id(poll_id: int, slot_id: int) -> str:
    return f"tp:{poll_id}:{slot_id}"


def parse_custom_id(value) -> tuple[int, int] | None:
    """(poll_id, slot_id), or None for anything that is not exactly our format."""
    match = _CUSTOM_ID.fullmatch(value) if isinstance(value, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else None


def voter_hash(poll_id: int, discord_user_id: str) -> str:
    """HMAC-SHA256(SECRET_KEY, "poll_id:discord_user_id"). Per poll, so one person is not linkable across polls."""
    key = os.environ.get("SECRET_KEY", "").encode()
    return hmac.new(key, f"{poll_id}:{discord_user_id}".encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SlotView:
    id: int
    starts_at: datetime
    votes: int


def leading_slots(slots: list[SlotView]) -> list[SlotView]:
    """The slots with the most votes (all of them on a tie). Empty when nobody voted."""
    top = max((s.votes for s in slots), default=0)
    return [s for s in slots if top and s.votes == top]


def _slot_label(index: int, starts_at: datetime) -> str:
    return f"{index}. {starts_at:%a %d %b %H:%M} UTC"


def _bar(votes: int, top: int) -> str:
    filled = round(BAR_WIDTH * votes / top) if top else 0
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def render_poll(title: str, slots: list[SlotView], status: str, closes_at: datetime,
                winner_slot_id: int | None = None) -> str:
    """The message text. Pure. Every slot shows a number beside its bar, never the bar alone."""
    top = max((s.votes for s in slots), default=0)
    lines = [f"**{escape_markdown(title)}**"]
    if status == "open":
        lines.append(f"Tap every time that works for you. Tap again to take your vote back. Closes <t:{int(closes_at.timestamp())}:R>.")
    elif status == "cancelled":
        lines.append("This poll was cancelled.")
    else:
        lines.append("This poll is closed.")
    lines.append("")
    for index, slot in enumerate(slots, start=1):
        mark = " ✅" if status == "closed" and slot.id == winner_slot_id else ""
        plural = "vote" if slot.votes == 1 else "votes"
        lines.append(f"{index}. <t:{int(slot.starts_at.timestamp())}:F> (`{slot.starts_at:%H:%M} UTC`)")
        lines.append(f"`{_bar(slot.votes, top)}` **{slot.votes}** {plural}{mark}")
    if status == "closed":
        leaders = leading_slots(slots)
        if winner_slot_id is None and len(leaders) == 1:
            lines.append(f"\nMost votes: option {slots.index(leaders[0]) + 1}.")
        elif winner_slot_id is None and len(leaders) > 1:
            lines.append("\nA tie between options " + ", ".join(str(slots.index(s) + 1) for s in leaders) + ".")
        elif winner_slot_id is None:
            lines.append("\nNo votes were cast.")
    return "\n".join(lines)


def build_components(poll_id: int, slots: list[SlotView]) -> list[dict]:
    """Up to 6 secondary buttons in rows of 5. Closed and cancelled polls pass no slots and get none."""
    buttons = [{"type": 2, "style": 2, "label": _slot_label(i, s.starts_at), "custom_id": custom_id(poll_id, s.id)}
               for i, s in enumerate(slots, start=1)]
    return [{"type": 1, "components": buttons[i:i + BUTTONS_PER_ROW]} for i in range(0, len(buttons), BUTTONS_PER_ROW)]


# ── Database ─────────────────────────────────────────────────

async def slot_views(db: AsyncSession, poll: TimePoll) -> list[SlotView]:
    counts = dict((await db.execute(
        select(TimePollVote.slot_id, func.count()).where(TimePollVote.slot_id.in_([s.id for s in poll.slots]))
        .group_by(TimePollVote.slot_id)
    )).all())
    return [SlotView(s.id, ensure_utc(s.starts_at), counts.get(s.id, 0)) for s in poll.slots]


async def poll_content(db: AsyncSession, poll: TimePoll) -> tuple[str, list[dict]]:
    views = await slot_views(db, poll)
    text = render_poll(poll.title, views, poll.status, ensure_utc(poll.closes_at), poll.winner_slot_id)
    return text, (build_components(poll.id, views) if poll.status == "open" else [])


async def record_vote(db: AsyncSession, poll_id: int, slot_id: int, discord_user_id: str, now: datetime) -> str:
    """Toggles one vote and commits. Returns "added", "removed", "closed" or "unknown"."""
    poll = await db.get(TimePoll, poll_id)
    if poll is None or not any(s.id == slot_id for s in poll.slots):
        return "unknown"
    if poll.status != "open" or ensure_utc(poll.closes_at) <= now:
        return "closed"
    who = voter_hash(poll_id, discord_user_id)
    existing = await db.get(TimePollVote, (slot_id, who))
    if existing is not None:
        await db.delete(existing)
        await db.commit()
        return "removed"
    db.add(TimePollVote(slot_id=slot_id, voter_hash=who))
    try:
        await db.commit()
    except IntegrityError:  # a concurrent identical click won; the vote is there either way
        await db.rollback()
    return "added"


# ── Interactions ─────────────────────────────────────────────

def _ephemeral(text: str) -> dict:
    return {"type": CALLBACK_CHANNEL_MESSAGE, "data": {"content": text, "flags": EPHEMERAL, "allowed_mentions": NO_MENTIONS}}


def _interaction_user(interaction: dict) -> str:
    user = (interaction.get("member") or {}).get("user") or interaction.get("user") or {}
    return str(user.get("id") or "")


async def handle_component(db: AsyncSession, interaction: dict, now: datetime | None = None) -> dict:
    """A button click. Answers with callback type 7 (edit the clicked message) carrying the fresh tally."""
    now = now or datetime.now(timezone.utc)
    parsed = parse_custom_id((interaction.get("data") or {}).get("custom_id"))
    user_id = _interaction_user(interaction)
    if parsed is None or not user_id:
        return _ephemeral("This poll is closed.")
    poll_id, slot_id = parsed
    if not _click_limit.allow(user_id):
        return _ephemeral("Slow down a little and try again in a minute.")
    poll = await db.get(TimePoll, poll_id)
    guild = str(interaction.get("guild_id") or "")
    if poll is None or not guild or guild not in {m.guild_id for m in poll.messages}:
        return _ephemeral("This poll is closed.")
    outcome = await record_vote(db, poll_id, slot_id, user_id, now)
    if outcome in ("closed", "unknown"):
        return _ephemeral("This poll is closed.")
    await db.refresh(poll)
    text, components = await poll_content(db, poll)
    message = interaction.get("message") or {}
    channel = str(interaction.get("channel_id") or message.get("channel_id") or "")
    row = next((m for m in poll.messages if m.channel_id == channel), None)
    if row is not None:  # this copy is already current, so the tick should not edit it again
        row.content_hash, row.refreshed_at, row.error = content_hash(text), now, None
        await db.commit()
    return {"type": CALLBACK_UPDATE_MESSAGE, "data": {"content": text, "components": components, "allowed_mentions": NO_MENTIONS}}


# ── Posting, refreshing and closing ──────────────────────────

async def _token_for(db: AsyncSession, guild_id: str) -> str:
    server = (await db.execute(select(DiscordServer).where(DiscordServer.guild_id == guild_id))).scalars().first()
    return (server.bot_token if server and server.bot_token else "") or platform_bot_token()


async def post_poll(session_factory, discord, poll_id: int, targets: list[tuple[int | None, str, str]], now: datetime) -> list[str]:
    """Posts the poll once per (destination_id, guild_id, channel_id) target, stores each message, and returns
    error texts for the ones that failed. The poll and its votes already exist, so a failure loses nothing."""
    errors: list[str] = []
    for destination_id, guild_id, channel_id in targets:
        async with session_factory() as db:
            poll = await db.get(TimePoll, poll_id)
            text, components = await poll_content(db, poll)
            token = await _token_for(db, guild_id)
        if not token:
            errors.append(f"Channel {channel_id}: no Discord bot token configured for this server")
            continue
        message_id, error = await discord.post_channel_message(token, channel_id, text, no_mentions=True, components=components)
        if not message_id:
            errors.append(f"Channel {channel_id}: {error}")
            continue
        async with session_factory() as db:
            db.add(TimePollMessage(poll_id=poll_id, destination_id=destination_id, guild_id=guild_id, channel_id=channel_id,
                                   message_id=message_id, content_hash=content_hash(text), refreshed_at=now))
            await db.commit()
    return errors


async def refresh_poll_messages(session_factory, discord, poll_id: int, now: datetime, force: bool = False) -> dict[str, int]:
    """Edits each copy of the poll whose text changed. Open polls are throttled per message; `force` skips the
    throttle and the error back-off. Never raises. Returns counts: edited, unchanged, skipped, error."""
    counts = {"edited": 0, "unchanged": 0, "skipped": 0, "error": 0}
    async with session_factory() as db:
        poll = await db.get(TimePoll, poll_id)
        if poll is None:
            return counts
        text, components = await poll_content(db, poll)
        new_hash = content_hash(text)
        status = poll.status
        copies = [(m.id, m.guild_id, m.channel_id, m.message_id, m.content_hash, m.error,
                   ensure_utc(m.refreshed_at) if m.refreshed_at else None) for m in poll.messages]
    for message_row_id, guild_id, channel_id, message_id, stored_hash, error, refreshed in copies:
        if stored_hash == new_hash and not error:
            counts["unchanged"] += 1
            continue
        if not force and refreshed is not None:
            wait = ERROR_RETRY_AFTER if error else (EDIT_THROTTLE if status == "open" else timedelta(0))
            if now - refreshed < wait:
                counts["skipped"] += 1
                continue
        outcome = "error"
        try:
            async with session_factory() as db:
                token = await _token_for(db, guild_id)
            if not token:
                problem = "No Discord bot token configured for this server"
            else:
                ok, problem = await discord.edit_channel_message(token, channel_id, message_id, text, no_mentions=True, components=components)
                if ok:
                    outcome, problem = "edited", None
                elif problem == MESSAGE_GONE:
                    problem = "The poll message was deleted in Discord"
        except Exception as exc:  # never let one channel stop the others
            logger.exception("poll %s message %s failed", poll_id, message_row_id)
            problem = f"{type(exc).__name__}: {exc}"
        async with session_factory() as db:
            row = await db.get(TimePollMessage, message_row_id)
            if row is not None:
                row.refreshed_at, row.error = now, (problem[:255] if problem else None)
                if outcome == "edited":
                    row.content_hash = new_hash
                await db.commit()
        counts[outcome] += 1
    return counts


async def close_poll(session_factory, discord, poll_id: int, now: datetime, winner_slot_id: int | None = None) -> bool:
    """Closes an open poll, removes its buttons and writes the final tally. False if it was not open."""
    async with session_factory() as db:
        poll = await db.get(TimePoll, poll_id)
        if poll is None or poll.status != "open":
            return False
        poll.status, poll.closed_at, poll.winner_slot_id = "closed", now, winner_slot_id
        await db.commit()
    await refresh_poll_messages(session_factory, discord, poll_id, now, force=True)
    return True


async def cancel_poll(session_factory, discord, poll_id: int, now: datetime) -> bool:
    async with session_factory() as db:
        poll = await db.get(TimePoll, poll_id)
        if poll is None or poll.status != "open":
            return False
        poll.status, poll.closed_at = "cancelled", now
        await db.commit()
    await refresh_poll_messages(session_factory, discord, poll_id, now, force=True)
    return True


async def run_poll_tick(session_factory, discord, now: datetime) -> dict[str, int]:
    """Per-minute pass: close expired polls, then bring changed messages up to date (at most BATCH_SIZE polls)."""
    totals = {"closed": 0, "edited": 0, "error": 0}
    async with session_factory() as db:
        expired = list((await db.execute(
            select(TimePoll.id).where(TimePoll.status == "open", TimePoll.closes_at <= now))).scalars())
    for poll_id in expired:
        if await close_poll(session_factory, discord, poll_id, now):
            totals["closed"] += 1
    async with session_factory() as db:
        live = list((await db.execute(
            select(TimePoll.id).where(
                (TimePoll.status == "open") | (TimePoll.closed_at >= now - FINAL_EDIT_WINDOW)
            ).order_by(TimePoll.id).limit(BATCH_SIZE))).scalars())
    for poll_id in live:
        result = await refresh_poll_messages(session_factory, discord, poll_id, now)
        totals["edited"] += result["edited"]
        totals["error"] += result["error"]
    return totals


async def prune_polls(session_factory, now: datetime) -> int:
    """Deletes polls older than RETENTION_DAYS with their slots, votes and messages."""
    async with session_factory() as db:
        old = list((await db.execute(
            select(TimePoll.id).where(TimePoll.created_at < now - timedelta(days=RETENTION_DAYS)))).scalars())
        if not old:
            return 0
        slot_ids = list((await db.execute(select(TimePollSlot.id).where(TimePollSlot.poll_id.in_(old)))).scalars())
        await db.execute(delete(TimePollVote).where(TimePollVote.slot_id.in_(slot_ids)))
        await db.execute(delete(TimePollMessage).where(TimePollMessage.poll_id.in_(old)))
        await db.execute(TimePoll.__table__.update().where(TimePoll.id.in_(old)).values(winner_slot_id=None))
        await db.execute(delete(TimePollSlot).where(TimePollSlot.poll_id.in_(old)))
        await db.execute(delete(TimePoll).where(TimePoll.id.in_(old)))
        await db.commit()
        return len(old)
