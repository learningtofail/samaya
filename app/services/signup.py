"""Notify me and I'm in (spec §87).

Two buttons on a reminder. **Notify me** toggles a Discord role for the player
and records the intent; **I'm in** records attendance at one occurrence. Both
store a voter as an HMAC of a scope and the Discord user, never the Discord ID,
and counts are shown only to leaders, on reminders and on the schedule board.

Clicks answer inside Discord's 3 seconds. Notify me answers with a deferred
private reply and returns a follow-up coroutine for the caller to run after the
response is sent; I'm in touches only the database and answers at once. Discord
and the session factory are injected like in `event_engine`, so tests use
`FakeDiscord`.
"""
import hashlib
import hmac
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import (
    Delivery, DiscordServer, Event, EventOccurrence, EventSubscription, OccurrenceRsvp, SignupGroup, SignupRole,
)
from services.event_engine import effective_start, platform_bot_token
from services.rate_limit import RateLimiter
from services.time_utils import ensure_utc

logger = logging.getLogger(__name__)

CALLBACK_CHANNEL_MESSAGE = 4
CALLBACK_DEFERRED_MESSAGE = 5
EPHEMERAL = 64
NO_MENTIONS = {"parse": []}

RSVP_RETENTION_DAYS = 30
REFRESH_BATCH = 20
REFRESH_THROTTLE = timedelta(minutes=2)
REFRESH_ERROR_RETRY = timedelta(minutes=10)
ROLE_NAME_MAX = 100
WINDOWS = ("none", "day", "week")

_SUB = re.compile(r"sub:([0-9]{1,9})")
_SWITCH = re.compile(r"sw:([0-9]{1,9})")
_IN = re.compile(r"in:([0-9]{1,9}):([0-9]{8})")
_RSVP_SWITCH = re.compile(r"rs:([0-9]{1,9}):([0-9]{8})")
_click_limit = RateLimiter("signup_click", max_requests=10, window_seconds=60)

MSG_INACTIVE = "This button is no longer active."
MSG_SLOW = "Slow down a little and try again in a minute."
MSG_UNSAFE = "That role cannot be given here, so ask a leader."
MSG_CANNOT = "Samaya cannot change roles in this server yet, so ask a leader."
MSG_FAILED = "Something went wrong changing your role. Try again in a minute."
MSG_STARTED = "This event has already started."
MSG_CANCELLED = "This occurrence was cancelled."

# Permission bits from Discord's documentation. A role holding any of these could be used to
# moderate the server, so Notify me never hands it out (spec §87.6). Confirm against the docs when changed.
DENIED_PERMISSIONS: dict[str, int] = {
    "Administrator": 1 << 3,
    "Manage Server": 1 << 5,
    "Manage Roles": 1 << 28,
    "Manage Channels": 1 << 4,
    "Manage Messages": 1 << 13,
    "Manage Webhooks": 1 << 29,
    "Manage Threads": 1 << 34,
    "Manage Nicknames": 1 << 27,
    "Manage Expressions": 1 << 30,
    "Manage Events": 1 << 33,
    "Kick Members": 1 << 1,
    "Ban Members": 1 << 2,
    "Moderate Members": 1 << 40,
    "View Audit Log": 1 << 7,
    "Mention Everyone": 1 << 17,
}

Followup = Callable[[], Awaitable[None]]


# ── Pure helpers ─────────────────────────────────────────────

def sub_custom_id(event_id: int) -> str:
    return f"sub:{event_id}"


def switch_custom_id(event_id: int) -> str:
    return f"sw:{event_id}"


def rsvp_custom_id(event_id: int, day: date) -> str:
    return f"in:{event_id}:{day:%Y%m%d}"


def rsvp_switch_custom_id(event_id: int, day: date) -> str:
    return f"rs:{event_id}:{day:%Y%m%d}"


def _parse_day(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _parse_event(pattern: re.Pattern, value) -> int | None:
    match = pattern.fullmatch(value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def _parse_event_day(pattern: re.Pattern, value) -> tuple[int, date] | None:
    match = pattern.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    day = _parse_day(match.group(2))
    return (int(match.group(1)), day) if day else None


def parse_sub(value) -> int | None:
    return _parse_event(_SUB, value)


def parse_switch(value) -> int | None:
    return _parse_event(_SWITCH, value)


def parse_rsvp(value) -> tuple[int, date] | None:
    return _parse_event_day(_IN, value)


def parse_rsvp_switch(value) -> tuple[int, date] | None:
    return _parse_event_day(_RSVP_SWITCH, value)


def _hmac(text: str) -> str:
    return hmac.new(os.environ.get("SECRET_KEY", "").encode(), text.encode(), hashlib.sha256).hexdigest()


def sub_hash(server_id: int, role_id: str, discord_user_id: str) -> str:
    return _hmac(f"sub:{server_id}:{role_id}:{discord_user_id}")


def rsvp_hash(group_id: int | None, event_id: int, discord_user_id: str) -> str:
    """One hash per player across an attendance group's events, so a clash can be found; per event otherwise."""
    scope = f"g{group_id}" if group_id else f"e{event_id}"
    return _hmac(f"rsvp:{scope}:{discord_user_id}")


def unsafe_role_reason(role: dict | None, bot_top_position: int | None, guild_id: str) -> str | None:
    """Why Notify me must not give this role, or None when it may. Pure."""
    if role is None:
        return "That role does not exist in this server"
    if str(role.get("id")) == str(guild_id):
        return "That is the @everyone role"
    if role.get("managed"):
        return "That role is managed by a bot or integration"
    if bot_top_position is not None and int(role.get("position", 0)) >= bot_top_position:
        return "That role is at or above the bot's highest role, so Samaya cannot give it"
    try:
        bits = int(str(role.get("permissions", "0")))
    except (TypeError, ValueError):
        return "That role's permissions could not be read"
    for label, bit in DENIED_PERMISSIONS.items():
        if bits & bit:
            return f"That role has the {label} permission, so anyone could use it to moderate"
    return None


def week_start(day: date) -> date:
    """Kingshot weeks start on Monday."""
    return day - timedelta(days=day.weekday())


def window_range(day: date, window: str) -> tuple[date, date] | None:
    """The inclusive date range one "I'm in" covers, or None when the group has no window."""
    if window == "day":
        return day, day
    if window == "week":
        start = week_start(day)
        return start, start + timedelta(days=6)
    return None


def count_line(count: int) -> str:
    return f"✅ **{count} going**" if count > 0 else ""


def with_count(base: str, count: int, limit: int = 2000) -> str:
    """The reminder text with its headcount line, cut so the whole message fits Discord's limit."""
    line = count_line(count)
    if not line:
        return base[:limit]
    return base[: limit - len(line) - 1] + "\n" + line


def button_row(event: Event, occurrence_date: date, notify: bool) -> list[dict]:
    """The action row for a reminder: I'm in, then Notify me. Empty when neither applies."""
    if event.leadership_only:
        return []
    buttons = []
    if event.rsvp_enabled:
        buttons.append({"type": 2, "style": 3, "label": "I'm in", "custom_id": rsvp_custom_id(event.id, occurrence_date)})
    if notify and event.signup_enabled:
        buttons.append({"type": 2, "style": 1, "label": "Notify me", "custom_id": sub_custom_id(event.id)})
    return [{"type": 1, "components": buttons}] if buttons else []


# ── Interaction plumbing ─────────────────────────────────────

def _ephemeral(text: str, components: list | None = None) -> dict:
    data = {"content": text, "flags": EPHEMERAL, "allowed_mentions": NO_MENTIONS}
    if components:
        data["components"] = components
    return {"type": CALLBACK_CHANNEL_MESSAGE, "data": data}


def _deferred() -> dict:
    return {"type": CALLBACK_DEFERRED_MESSAGE, "data": {"flags": EPHEMERAL}}


def _user_id(interaction: dict) -> str:
    user = (interaction.get("member") or {}).get("user") or interaction.get("user") or {}
    return str(user.get("id") or "")


def _held_roles(interaction: dict) -> set[str]:
    return {str(r) for r in ((interaction.get("member") or {}).get("roles") or [])}


def _when(start: datetime) -> str:
    return f"{start:%a %d %b %H:%M} UTC"


def _switch_button(custom_id: str) -> list[dict]:
    return [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Switch", "custom_id": custom_id}]}]


# ── Roles ────────────────────────────────────────────────────

async def servers_for_guild(db: AsyncSession, guild_id: str) -> list[DiscordServer]:
    return list((await db.execute(select(DiscordServer).where(DiscordServer.guild_id == guild_id))).scalars().all())


async def resolve_signup_role(db: AsyncSession, event: Event, guild_id: str) -> tuple[SignupRole, DiscordServer] | None:
    """The role Notify me gives for this event in this Discord server: the event's own, else its type's."""
    servers = {s.id: s for s in await servers_for_guild(db, guild_id)}
    if not servers:
        return None
    rows = (await db.execute(select(SignupRole).where(
        SignupRole.server_id.in_(list(servers)),
        or_(SignupRole.event_id == event.id, SignupRole.type_id == event.type_id)))).scalars().all()
    if not rows:
        return None
    best = next((r for r in rows if r.event_id == event.id), rows[0])
    return best, servers[best.server_id]


async def resolve_signup_role_for_server(db: AsyncSession, event: Event, server_id: int) -> SignupRole | None:
    rows = (await db.execute(select(SignupRole).where(
        SignupRole.server_id == server_id,
        or_(SignupRole.event_id == event.id, SignupRole.type_id == event.type_id)))).scalars().all()
    return next((r for r in rows if r.event_id == event.id), rows[0] if rows else None)


async def group_peers(db: AsyncSession, event: Event, guild_id: str) -> list[tuple[Event, SignupRole, DiscordServer]]:
    """The other events of this event's exclusive-roles group that have a role in this server."""
    if not event.signup_group_id:
        return []
    group = await db.get(SignupGroup, event.signup_group_id)
    if group is None or not group.exclusive_roles:
        return []
    peers = (await db.execute(select(Event).where(
        Event.signup_group_id == group.id, Event.id != event.id, Event.active == True))).scalars().all()  # noqa: E712
    out = []
    for peer in peers:
        resolved = await resolve_signup_role(db, peer, guild_id)
        if resolved is not None:
            out.append((peer, resolved[0], resolved[1]))
    return out


def _bot_token(server: DiscordServer) -> str:
    return server.bot_token or platform_bot_token()


async def _store_error(session_factory, mapping_id: int, error: str | None, now: datetime) -> None:
    async with session_factory() as db:
        row = await db.get(SignupRole, mapping_id)
        if row is not None:
            row.last_error, row.last_error_at = (error[:500] if error else None), (now if error else None)
            await db.commit()


async def _set_subscription(db: AsyncSession, server_id: int, role_id: str, user_id: str, on: bool) -> None:
    who = sub_hash(server_id, role_id, user_id)
    existing = await db.get(EventSubscription, (server_id, role_id, who))
    if on and existing is None:
        db.add(EventSubscription(server_id=server_id, role_id=role_id, voter_hash=who))
        try:
            await db.commit()
        except IntegrityError:  # a concurrent identical tap won
            await db.rollback()
    elif not on and existing is not None:
        await db.delete(existing)
        await db.commit()


async def subscriber_count(db: AsyncSession, server_id: int, role_id: str) -> int:
    return (await db.execute(select(func.count()).select_from(EventSubscription).where(
        EventSubscription.server_id == server_id, EventSubscription.role_id == role_id))).scalar_one()


def _failure_text(error: str) -> str:
    return MSG_CANNOT if error.startswith(("401", "403")) else MSG_FAILED


# ── Notify me ────────────────────────────────────────────────

async def _event_ok(db: AsyncSession, event_id: int, guild_id: str, need: str) -> Event | None:
    event = await db.get(Event, event_id)
    if event is None or not event.active or event.leadership_only or not guild_id:
        return None
    if need == "signup" and not event.signup_enabled:
        return None
    if need == "rsvp" and not event.rsvp_enabled:
        return None
    return event


async def handle_notify(db: AsyncSession, interaction: dict, discord, session_factory,
                        now: datetime, switch: bool = False) -> tuple[dict, Followup | None]:
    """`sub:{event}` (a toggle) or `sw:{event}` (confirmed switch). Answers with a deferred private reply and
    returns the follow-up that changes the role and edits that reply."""
    data = interaction.get("data") or {}
    event_id = parse_switch(data.get("custom_id")) if switch else parse_sub(data.get("custom_id"))
    user_id = _user_id(interaction)
    if event_id is None or not user_id:
        return _ephemeral(MSG_INACTIVE), None
    if not _click_limit.allow(user_id):
        return _ephemeral(MSG_SLOW), None
    guild_id = str(interaction.get("guild_id") or "")
    event = await _event_ok(db, event_id, guild_id, "signup")
    resolved = await resolve_signup_role(db, event, guild_id) if event else None
    if event is None or resolved is None:
        return _ephemeral(MSG_INACTIVE), None
    mapping, _server = resolved
    held = _held_roles(interaction)
    if not switch and mapping.role_id not in held:
        clashes = [peer for peer, peer_map, _s in await group_peers(db, event, guild_id) if peer_map.role_id in held]
        if clashes:
            return _ephemeral(f"You are already in {clashes[0].name}. Switch to {event.name}?",
                              _switch_button(switch_custom_id(event.id))), None
    application_id, itoken = str(interaction.get("application_id") or ""), str(interaction.get("token") or "")

    async def followup() -> None:
        try:
            text = await _apply_notify(session_factory, discord, event_id, guild_id, user_id, held, switch, now)
        except Exception:  # the reply must always be edited
            logger.exception("notify me failed for event %s", event_id)
            text = MSG_FAILED
        ok, error = await discord.edit_interaction_response(application_id, itoken, text)
        if not ok:
            logger.warning("could not edit the Notify me reply: %s", error)

    return _deferred(), followup


async def _apply_notify(session_factory, discord, event_id: int, guild_id: str, user_id: str,
                        held: set[str], switch: bool, now: datetime) -> str:
    async with session_factory() as db:
        event = await _event_ok(db, event_id, guild_id, "signup")
        resolved = await resolve_signup_role(db, event, guild_id) if event else None
        if event is None or resolved is None:
            return MSG_INACTIVE
        mapping, server = resolved
        token = _bot_token(server)
        if not token:
            return MSG_CANNOT
        mapping_id, role_id, server_id, name = mapping.id, mapping.role_id, server.id, event.name
        peers = [(p.name, m.role_id, m.server_id) for p, m, _s in await group_peers(db, event, guild_id)
                 if m.role_id in held]

        if role_id in held and not switch:
            ok, error = await discord.remove_member_role(token, guild_id, user_id, role_id, f"Notify me: {name}")
            if not ok:
                await _store_error(session_factory, mapping_id, error, now)
                return _failure_text(error)
            await _set_subscription(db, server_id, role_id, user_id, False)
            await _store_error(session_factory, mapping_id, None, now)
            return f"You will no longer be notified about {name}."

        roles, bot_top, context_error = await discord.get_role_context(token, guild_id)
        if context_error:
            await _store_error(session_factory, mapping_id, context_error, now)
            return _failure_text(context_error)
        reason = unsafe_role_reason(roles.get(role_id), bot_top, guild_id)
        if reason:
            await _store_error(session_factory, mapping_id, f"Refused: {reason}", now)
            return MSG_UNSAFE

        removed: list[tuple[str, int]] = []
        if switch:
            for _peer_name, peer_role, peer_server in peers:
                ok, error = await discord.remove_member_role(token, guild_id, user_id, peer_role, f"Switch to {name}")
                if not ok:
                    for restore, _srv in removed:
                        await discord.add_member_role(token, guild_id, user_id, restore, "Switch failed")
                    await _store_error(session_factory, mapping_id, error, now)
                    return _failure_text(error)
                removed.append((peer_role, peer_server))
        ok, error = await discord.add_member_role(token, guild_id, user_id, role_id, f"Notify me: {name}")
        if not ok:
            for restore, _srv in removed:  # put back what the switch took away
                await discord.add_member_role(token, guild_id, user_id, restore, "Switch failed")
            await _store_error(session_factory, mapping_id, error, now)
            return _failure_text(error)
        for peer_role, peer_server in removed:
            await _set_subscription(db, peer_server, peer_role, user_id, False)
        await _set_subscription(db, server_id, role_id, user_id, True)
        await _store_error(session_factory, mapping_id, None, now)
        return f"You will now be notified about {name}."


# ── I'm in ───────────────────────────────────────────────────

async def _load_rsvp_target(db: AsyncSession, event_id: int, day: date, guild_id: str,
                            now: datetime) -> tuple[Event, EventOccurrence] | str:
    """(event, occurrence), or the private message to answer with."""
    event = await _event_ok(db, event_id, guild_id, "rsvp")
    if event is None:
        return MSG_INACTIVE
    occ = (await db.execute(select(EventOccurrence).where(
        EventOccurrence.event_id == event_id, EventOccurrence.occurrence_date == day))).scalar_one_or_none()
    if occ is None:
        return MSG_INACTIVE
    if occ.status == "cancelled":
        return MSG_CANCELLED
    if ensure_utc(effective_start(occ)) <= now:
        return MSG_STARTED
    posted_here = (await db.execute(select(func.count()).select_from(Delivery).where(
        Delivery.occurrence_id == occ.id, Delivery.kind == "reminder", Delivery.guild_id == guild_id))).scalar_one()
    if not posted_here:
        return MSG_INACTIVE
    return event, occ


async def rsvp_counts(db: AsyncSession, keys: list[tuple[int, date]]) -> dict[tuple[int, date], int]:
    """Headcount per (event_id, occurrence_date). Counts only; no hashes leave this function."""
    if not keys:
        return {}
    wanted = set(keys)
    rows = (await db.execute(
        select(OccurrenceRsvp.event_id, OccurrenceRsvp.occurrence_date, func.count())
        .where(OccurrenceRsvp.event_id.in_({k[0] for k in keys}),
               OccurrenceRsvp.occurrence_date >= min(k[1] for k in keys),
               OccurrenceRsvp.occurrence_date <= max(k[1] for k in keys))
        .group_by(OccurrenceRsvp.event_id, OccurrenceRsvp.occurrence_date))).all()
    return {(e, d): n for e, d, n in rows if (e, d) in wanted}


async def _clashes(db: AsyncSession, event: Event, day: date, who: str) -> list[tuple[Event, date]]:
    """Other occurrences in the event's group and window this player already said "I'm in" to."""
    if not event.signup_group_id:
        return []
    group = await db.get(SignupGroup, event.signup_group_id)
    span = window_range(day, group.attendance_window) if group else None
    if span is None:
        return []
    ids = list((await db.execute(select(Event.id).where(Event.signup_group_id == group.id))).scalars().all())
    rows = (await db.execute(
        select(OccurrenceRsvp.event_id, OccurrenceRsvp.occurrence_date)
        .join(EventOccurrence, (EventOccurrence.event_id == OccurrenceRsvp.event_id)
              & (EventOccurrence.occurrence_date == OccurrenceRsvp.occurrence_date))
        .where(OccurrenceRsvp.voter_hash == who, OccurrenceRsvp.event_id.in_(ids),
               OccurrenceRsvp.occurrence_date >= span[0], OccurrenceRsvp.occurrence_date <= span[1],
               EventOccurrence.status == "scheduled")
        .order_by(OccurrenceRsvp.occurrence_date))).all()
    out = []
    for event_id, when in rows:
        if (event_id, when) != (event.id, day):
            out.append((await db.get(Event, event_id), when))
    return out


async def handle_rsvp(db: AsyncSession, interaction: dict, now: datetime, switch: bool = False) -> dict:
    """`in:{event}:{date}` toggles; `rs:{event}:{date}` confirms a switch. Database only, so it answers at once."""
    data = interaction.get("data") or {}
    parsed = parse_rsvp_switch(data.get("custom_id")) if switch else parse_rsvp(data.get("custom_id"))
    user_id = _user_id(interaction)
    if parsed is None or not user_id:
        return _ephemeral(MSG_INACTIVE)
    if not _click_limit.allow(user_id):
        return _ephemeral(MSG_SLOW)
    event_id, day = parsed
    guild_id = str(interaction.get("guild_id") or "")
    target = await _load_rsvp_target(db, event_id, day, guild_id, now)
    if isinstance(target, str):
        return _ephemeral(target)
    event, occ = target
    who = rsvp_hash(event.signup_group_id, event.id, user_id)
    label = f"{event.name} on {_when(ensure_utc(effective_start(occ)))}"
    existing = await db.get(OccurrenceRsvp, (event.id, day, who))
    clashes = await _clashes(db, event, day, who)

    if existing is not None and not switch:
        await db.delete(existing)
        await db.commit()
        return _ephemeral(f"You are out of {label}.")
    if clashes and not switch:
        other, other_day = clashes[0]
        return _ephemeral(f"You are already in {other.name} on {other_day:%a %d %b}. Switch to {event.name}?",
                          _switch_button(rsvp_switch_custom_id(event.id, day)))
    for other, other_day in clashes:
        row = await db.get(OccurrenceRsvp, (other.id, other_day, who))
        if row is not None:
            await db.delete(row)
    if existing is None:
        db.add(OccurrenceRsvp(event_id=event.id, occurrence_date=day, voter_hash=who))
    try:
        await db.commit()
    except IntegrityError:  # a concurrent identical tap won; the RSVP is there either way
        await db.rollback()
    count = (await rsvp_counts(db, [(event.id, day)])).get((event.id, day), 0)
    verb = "Switched to" if switch and clashes else "You are in for"
    return _ephemeral(f"{verb} {label}. {count} going.")


# ── Dispatch ─────────────────────────────────────────────────

async def handle_signup_component(db: AsyncSession, interaction: dict, discord, session_factory,
                                  now: datetime) -> tuple[dict, Followup | None] | None:
    """Routes a button click for this feature, or returns None when the custom_id is not ours."""
    custom_id = (interaction.get("data") or {}).get("custom_id")
    if not isinstance(custom_id, str):
        return None
    if custom_id.startswith("sub:"):
        return await handle_notify(db, interaction, discord, session_factory, now)
    if custom_id.startswith("sw:"):
        return await handle_notify(db, interaction, discord, session_factory, now, switch=True)
    if custom_id.startswith("in:"):
        return await handle_rsvp(db, interaction, now), None
    if custom_id.startswith("rs:"):
        return await handle_rsvp(db, interaction, now, switch=True), None
    return None


# ── Headcount on reminders ───────────────────────────────────

@dataclass
class _Candidate:
    delivery_id: int
    event_id: int
    day: date
    base: str
    shown: int
    channel_id: str
    message_id: str
    token: str
    server_id: int | None


async def run_rsvp_refresh(session_factory, discord, now: datetime) -> dict[str, int]:
    """Edits sent reminders whose headcount changed (spec §87.10 decision 10). At most REFRESH_BATCH edits per
    call, a 2 minute throttle per message, a 10 minute back-off after an error, and nothing after the event starts.
    Never raises. Returns counts: edited, unchanged, skipped, error."""
    counts = {"edited": 0, "unchanged": 0, "skipped": 0, "error": 0}
    async with session_factory() as db:
        rows = (await db.execute(
            select(Delivery, EventOccurrence, Event)
            .join(EventOccurrence, EventOccurrence.id == Delivery.occurrence_id)
            .join(Event, Event.id == EventOccurrence.event_id)
            .where(Delivery.kind == "reminder", Delivery.status == "posted", Delivery.discord_message_id.is_not(None),
                   Delivery.merged_into_id.is_(None), Delivery.message_deleted_at.is_(None),
                   Delivery.rsvp_base_content.is_not(None), Event.rsvp_enabled == True,  # noqa: E712
                   Event.leadership_only == False, Event.active == True,  # noqa: E712
                   EventOccurrence.status == "scheduled",
                   Delivery.posted_at_utc >= now - timedelta(days=14))
        )).all()
        live = [(d, o, e) for d, o, e in rows if ensure_utc(effective_start(o)) > now]
        counted = await rsvp_counts(db, [(e.id, o.occurrence_date) for _d, o, e in live])
        todo = []
        for delivery, occ, event in live:
            current = counted.get((event.id, occ.occurrence_date), 0)
            if current == (delivery.rsvp_count_shown or 0):
                counts["unchanged"] += 1
                continue
            edited = ensure_utc(delivery.rsvp_edited_at) if delivery.rsvp_edited_at else None
            failed = ensure_utc(delivery.rsvp_edit_error_at) if delivery.rsvp_edit_error_at else None
            if (edited and now - edited < REFRESH_THROTTLE) or (failed and now - failed < REFRESH_ERROR_RETRY):
                counts["skipped"] += 1
                continue
            dest = delivery.destination
            if dest is None or dest.server is None:
                counts["skipped"] += 1
                continue
            notify = await resolve_signup_role_for_server(db, event, dest.server_id) is not None
            todo.append((delivery.id, event, occ.occurrence_date, delivery.rsvp_base_content, current,
                         delivery.channel_id or dest.channel_id, delivery.discord_message_id,
                         _bot_token(dest.server), notify))
    for delivery_id, event, day, base, current, channel_id, message_id, token, notify in todo[:REFRESH_BATCH]:
        outcome, problem = "error", ""
        try:
            if not token:
                problem = "No Discord bot token configured for this server"
            else:
                ok, problem = await discord.edit_channel_message(
                    token, channel_id, message_id, with_count(base, current), no_mentions=True,
                    components=button_row(event, day, notify))
                outcome = "edited" if ok else "error"
        except Exception as exc:  # one message must not stop the rest
            logger.exception("headcount edit failed for delivery %s", delivery_id)
            problem = f"{type(exc).__name__}: {exc}"
        counts["edited" if outcome == "edited" else "error"] += 1
        async with session_factory() as db:
            row = await db.get(Delivery, delivery_id)
            if row is None:
                continue
            if outcome == "edited":
                row.rsvp_count_shown, row.rsvp_edited_at, row.rsvp_edit_error_at = current, now, None
            elif problem == "MESSAGE_GONE":  # deleted by hand or by cleanup: stop editing it
                row.message_deleted_at, row.cleanup_error = now, "The message no longer exists"
            else:
                row.rsvp_edit_error_at = now
            await db.commit()
    counts["skipped"] += max(0, len(todo) - REFRESH_BATCH)
    return counts


async def prune_rsvps(db: AsyncSession, now: datetime) -> int:
    """Deletes RSVPs for occurrences more than 30 days old (spec §87.10 decision 8)."""
    cutoff = now.astimezone(timezone.utc).date() - timedelta(days=RSVP_RETENTION_DAYS)
    result = await db.execute(delete(OccurrenceRsvp).where(OccurrenceRsvp.occurrence_date < cutoff))
    await db.commit()
    return result.rowcount or 0


# ── Mappings for the admin API ───────────────────────────────

async def remove_mapping(db: AsyncSession, mapping: SignupRole) -> None:
    """Deletes a mapping, and the subscriptions of its role when no other mapping still uses that role."""
    server_id, role_id = mapping.server_id, mapping.role_id
    await db.delete(mapping)
    await db.flush()
    still_used = (await db.execute(select(func.count()).select_from(SignupRole).where(
        SignupRole.server_id == server_id, SignupRole.role_id == role_id))).scalar_one()
    if not still_used:
        await db.execute(delete(EventSubscription).where(
            EventSubscription.server_id == server_id, EventSubscription.role_id == role_id))


async def drop_event_signup(db: AsyncSession, event_id: int) -> None:
    """Called before an event is deleted: its own role mappings, their orphaned subscriptions and its RSVPs."""
    for mapping in (await db.execute(select(SignupRole).where(SignupRole.event_id == event_id))).scalars().all():
        await remove_mapping(db, mapping)
    await db.execute(delete(OccurrenceRsvp).where(OccurrenceRsvp.event_id == event_id))


async def copy_event_signup(db: AsyncSession, source_id: int, target: Event, from_date: date) -> None:
    """A "this and following" split (spec §87.2 decision 2): the new series gets the same role mappings, which
    keeps the subscribers (they are keyed by role), and takes over the RSVPs from `from_date` on."""
    for mapping in (await db.execute(select(SignupRole).where(SignupRole.event_id == source_id))).scalars().all():
        db.add(SignupRole(event_id=target.id, server_id=mapping.server_id, role_id=mapping.role_id,
                          role_name=mapping.role_name, created_by_samaya=mapping.created_by_samaya))
    rows = (await db.execute(select(OccurrenceRsvp).where(
        OccurrenceRsvp.event_id == source_id, OccurrenceRsvp.occurrence_date >= from_date))).scalars().all()
    for row in rows:
        db.add(OccurrenceRsvp(event_id=target.id, occurrence_date=row.occurrence_date, voter_hash=row.voter_hash))
        await db.delete(row)
    await db.flush()


async def mapping_view(db: AsyncSession, mapping: SignupRole, server: DiscordServer) -> dict:
    return {
        "scope": "event" if mapping.event_id is not None else "type",
        "server_id": server.id, "server_name": server.name,
        "role_id": mapping.role_id, "role_name": mapping.role_name,
        "created_by_samaya": mapping.created_by_samaya,
        "subscribers": await subscriber_count(db, server.id, mapping.role_id),
        "last_error": mapping.last_error,
    }


async def signup_overview(db: AsyncSession, event: Event, posting: list[dict]) -> dict:
    """The Notify me roles in effect for an event, one row per server it posts to or has a role in,
    and a warning for each posting server without one (spec §87.5). Counts only."""
    posting_ids = {s["id"] for s in posting}
    mappings = (await db.execute(select(SignupRole).where(
        or_(SignupRole.event_id == event.id, SignupRole.type_id == event.type_id)))).scalars().all()
    server_ids = posting_ids | {m.server_id for m in mappings}
    if not server_ids:
        return {"signup_roles": [], "signup_warnings": []}
    servers = {s.id: s for s in (await db.execute(select(DiscordServer).where(DiscordServer.id.in_(server_ids)))).scalars()}
    roles, warnings = [], []
    for server_id in sorted(server_ids):
        server = servers.get(server_id)
        if server is None:
            continue
        here = [m for m in mappings if m.server_id == server_id]
        chosen = next((m for m in here if m.event_id == event.id), here[0] if here else None)
        if chosen is not None:
            roles.append(await mapping_view(db, chosen, server))
            for peer, peer_mapping, _srv in await group_peers(db, event, server.guild_id):
                if peer_mapping.role_id == chosen.role_id:
                    warnings.append(f"{peer.name} uses the same role in {server.name}, so Switch cannot tell the two apart. "
                                    "Give each event in the group its own role.")
        else:
            roles.append({"scope": None, "server_id": server.id, "server_name": server.name, "role_id": None,
                          "role_name": None, "created_by_samaya": False, "subscribers": 0, "last_error": None})
            if event.signup_enabled and not event.leadership_only and server_id in posting_ids:
                warnings.append(f"{server.name} has no Notify me role, so reminders there have no Notify me button.")
    return {"signup_roles": roles, "signup_warnings": warnings}


async def type_signup_roles(db: AsyncSession, type_id: int) -> list[dict]:
    """The event type's own role mappings, one per server."""
    rows = (await db.execute(
        select(SignupRole, DiscordServer).join(DiscordServer, DiscordServer.id == SignupRole.server_id)
        .where(SignupRole.type_id == type_id).order_by(DiscordServer.name))).all()
    return [await mapping_view(db, mapping, server) for mapping, server in rows]
