"""Private menus under the schedule board (spec §82.10).

The board message carries two buttons, Notify me and I'm in. Each opens a private
multi-select menu that lists the board's events (Notify me) or occurrences
(I'm in) with the player's current state ticked. Submitting the menu applies the
difference with the same rules as the reminder buttons: role safety, exclusive
groups, attendance windows and the shared click limit. A menu is a snapshot, so
the offered list is recomputed on submit and anything no longer offered is ignored.
"""
import logging
import re
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import AudienceDestination, Event, OccurrenceRsvp
from services import signup
from services.schedule_board import board_lines, parse_board_button
from services.time_utils import ensure_utc

logger = logging.getLogger(__name__)

CALLBACK_UPDATE_MESSAGE = 7
MAX_OPTIONS = 25
LABEL_MAX = 100
_MENU = re.compile(r"bm:(n|i):([0-9]{1,9})")
MSG_EMPTY = "There is nothing to choose from on this board right now."
_NOTIFY_OK = ("You will now be notified", "You will no longer be notified")


def menu_custom_id(kind: str, destination_id: int) -> str:
    return f"bm:{kind}:{destination_id}"


def parse_menu(value) -> tuple[str, int] | None:
    match = _MENU.fullmatch(value) if isinstance(value, str) else None
    return (match.group(1), int(match.group(2))) if match else None


def _update(text: str) -> dict:
    """Replaces the private menu with a text answer and no components."""
    return {"type": CALLBACK_UPDATE_MESSAGE, "data": {
        "content": text, "components": [], "allowed_mentions": signup.NO_MENTIONS}}


def _menu(kind: str, destination_id: int, intro: str, placeholder: str, options: list[dict]) -> dict:
    select = {"type": 3, "custom_id": menu_custom_id(kind, destination_id), "placeholder": placeholder,
              "min_values": 0, "max_values": len(options), "options": options}
    return signup._ephemeral(intro, [{"type": 1, "components": [select]}])


def _day_text(start: datetime) -> str:
    return f"{start:%a %d %b %H:%M} UTC"


async def _destination(db: AsyncSession, destination_id: int, guild_id: str) -> AudienceDestination | None:
    dest = await db.get(AudienceDestination, destination_id)
    if dest is None or dest.board_scope is None or not guild_id or dest.server.guild_id != guild_id:
        return None
    return dest


# ── Offered lists ────────────────────────────────────────────

async def offered_events(db: AsyncSession, dest: AudienceDestination, guild_id: str,
                         now: datetime) -> list[tuple[int, str, str]]:
    """(event id, name, role id) for events on the board that have a Notify me role in this server."""
    _title, lines = await board_lines(db, dest, now)
    seen: dict[int, tuple[int, str, str]] = {}
    for line in lines:
        if line.cancelled or not line.signup or line.event_id is None or line.event_id in seen:
            continue
        event = await signup._event_ok(db, line.event_id, guild_id, "signup")
        resolved = await signup.resolve_signup_role(db, event, guild_id) if event else None
        if resolved is not None:
            seen[line.event_id] = (event.id, event.name, resolved[0].role_id)
    return sorted(seen.values(), key=lambda e: (e[1].lower(), e[0]))[:MAX_OPTIONS]


async def offered_occurrences(db: AsyncSession, dest: AudienceDestination, guild_id: str, user_id: str,
                              now: datetime) -> list[tuple[Event, date, datetime, bool]]:
    """(event, day, start, already in) for upcoming occurrences on the board that take attendance here."""
    _title, lines = await board_lines(db, dest, now)
    out = []
    for line in sorted(lines, key=lambda x: x.start):
        if line.cancelled or not line.rsvp or line.event_id is None or line.day is None:
            continue
        target = await signup._load_rsvp_target(db, line.event_id, line.day, guild_id, now)
        if isinstance(target, str):
            continue
        event, occ = target
        who = signup.rsvp_hash(event.signup_group_id, event.id, user_id)
        have = await db.get(OccurrenceRsvp, (event.id, line.day, who)) is not None
        out.append((event, line.day, ensure_utc(line.start), have))
    return out[:MAX_OPTIONS]


# ── Entry points ─────────────────────────────────────────────

async def handle_board_component(db: AsyncSession, interaction: dict, discord, session_factory,
                                 now: datetime):
    """Routes a board button or menu submit, or returns None when the custom_id is not ours."""
    data = interaction.get("data") or {}
    custom_id = data.get("custom_id")
    button = parse_board_button(custom_id)
    menu = parse_menu(custom_id)
    if button is None and menu is None:
        return None
    user_id = signup._user_id(interaction)
    guild_id = str(interaction.get("guild_id") or "")
    if not user_id:
        return signup._ephemeral(signup.MSG_INACTIVE), None
    if not signup._click_limit.allow(user_id):
        return signup._ephemeral(signup.MSG_SLOW), None
    kind, destination_id = (("n" if button[0] == "bn" else "i"), button[1]) if button else menu
    dest = await _destination(db, destination_id, guild_id)
    if dest is None:
        return signup._ephemeral(signup.MSG_INACTIVE), None
    try:
        if button:
            return await _open_menu(db, interaction, dest, kind, guild_id, user_id, now), None
        values = [v for v in (data.get("values") or []) if isinstance(v, str)]
        if kind == "n":
            return await _submit_notify(db, interaction, dest, guild_id, user_id, values, discord, session_factory, now)
        return await _submit_rsvp(db, dest, guild_id, user_id, values, now), None
    except ValueError:  # the board's scope was removed
        return signup._ephemeral(signup.MSG_INACTIVE), None


async def _open_menu(db, interaction, dest, kind, guild_id, user_id, now) -> dict:
    if kind == "n":
        held = signup._held_roles(interaction)
        events = await offered_events(db, dest, guild_id, now)
        if not events:
            return signup._ephemeral(MSG_EMPTY)
        options = [{"label": name[:LABEL_MAX], "value": str(event_id), "default": role_id in held}
                   for event_id, name, role_id in events]
        return _menu("n", dest.id, "Tick the events you want to be notified about. Untick to stop.",
                     "Choose events", options)
    items = await offered_occurrences(db, dest, guild_id, user_id, now)
    if not items:
        return signup._ephemeral(MSG_EMPTY)
    options = [{"label": f"{event.name} · {_day_text(start)}"[:LABEL_MAX], "value": f"{event.id}:{day:%Y%m%d}",
                "default": have} for event, day, start, have in items]
    return _menu("i", dest.id, "Tick the events you are in for. Untick to withdraw.", "Choose events", options)


# ── Submit: Notify me ────────────────────────────────────────

async def _submit_notify(db, interaction, dest, guild_id, user_id, values, discord, session_factory, now):
    events = await offered_events(db, dest, guild_id, now)
    chosen = set(values)
    dest_id = dest.id
    held = signup._held_roles(interaction)
    application_id, itoken = str(interaction.get("application_id") or ""), str(interaction.get("token") or "")
    removes = [e for e in events if e[2] in held and str(e[0]) not in chosen]
    adds = [e for e in events if e[2] not in held and str(e[0]) in chosen]
    if not removes and not adds:
        return _update("Nothing changed."), None

    async def followup() -> None:
        lines: list[str] = []
        current = set(held)
        try:
            for event_id, name, role_id in removes:
                text = await signup._apply_notify(session_factory, discord, event_id, guild_id, user_id,
                                                  set(current), False, now)
                if text.startswith(_NOTIFY_OK):
                    current.discard(role_id)
                    lines.append(f"No longer notified: {name}")
                else:
                    lines.append(f"{name}: {text}")
            for event_id, name, role_id in adds:
                async with session_factory() as session:
                    event = await session.get(Event, event_id)
                    peers = [p.name for p, m, _s in await signup.group_peers(session, event, guild_id)
                             if m.role_id in current] if event else []
                if peers:
                    lines.append(f"Skipped {name}: you already have {peers[0]}. Untick it first.")
                    continue
                text = await signup._apply_notify(session_factory, discord, event_id, guild_id, user_id,
                                                  set(current), False, now)
                if text.startswith(_NOTIFY_OK):
                    current.add(role_id)
                    lines.append(f"Notified: {name}")
                else:
                    lines.append(f"{name}: {text}")
        except Exception:  # the reply must always be edited
            logger.exception("board Notify me failed for destination %s", dest_id)
            lines.append(signup.MSG_FAILED)
        ok, error = await discord.edit_interaction_response(application_id, itoken, "\n".join(lines))
        if not ok:
            logger.warning("could not edit the board Notify me reply: %s", error)

    return _update("Updating your roles..."), followup


# ── Submit: I'm in ───────────────────────────────────────────

async def _submit_rsvp(db, dest, guild_id, user_id, values, now) -> dict:
    items = await offered_occurrences(db, dest, guild_id, user_id, now)
    chosen = set(values)
    removes = [i for i in items if i[3] and f"{i[0].id}:{i[1]:%Y%m%d}" not in chosen]
    adds = [i for i in items if not i[3] and f"{i[0].id}:{i[1]:%Y%m%d}" in chosen]
    if not removes and not adds:
        return _update("Nothing changed.")
    lines: list[str] = []
    for event, day, start, _have in removes:
        who = signup.rsvp_hash(event.signup_group_id, event.id, user_id)
        row = await db.get(OccurrenceRsvp, (event.id, day, who))
        if row is not None:
            await db.delete(row)
            await db.commit()
        lines.append(f"Out: {event.name} on {_day_text(start)}")
    for event, day, start, _have in adds:
        who = signup.rsvp_hash(event.signup_group_id, event.id, user_id)
        clashes = await signup._clashes(db, event, day, who)
        if clashes:
            other, other_day = clashes[0]
            lines.append(f"Skipped {event.name} on {_day_text(start)}: you are already in {other.name} on "
                         f"{other_day:%a %d %b}. Untick it first.")
            continue
        db.add(OccurrenceRsvp(event_id=event.id, occurrence_date=day, voter_hash=who))
        try:
            await db.commit()
        except IntegrityError:  # a concurrent identical submit won; the RSVP is there either way
            await db.rollback()
        lines.append(f"In: {event.name} on {_day_text(start)}")
    return _update("\n".join(lines))
