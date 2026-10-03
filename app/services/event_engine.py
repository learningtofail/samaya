"""Delivery engine for the unified event model (spec §66.4).

Three responsibilities, all driven by the `deliveries` table:

1. Generation. `sync_event_occurrences` makes `event_occurrences` and
   `deliveries` match an Event definition for a rolling window. It is
   idempotent and never touches cancelled or start-overridden occurrences.
2. Sending. `run_delivery_tick` (every minute) claims each due `pending`
   delivery with an atomic UPDATE, commits the claim, and only then calls
   Discord, so a crash or a second worker can never send twice. A claimed row
   that never finishes becomes `error` and is never retried automatically,
   because Discord may already have the message.
3. Edits. Helpers that update or remove already-posted Discord Scheduled
   Events when an occurrence is moved, cancelled or its event deleted.

Discord is injected (`discord`): any object exposing create_discord_event,
update_discord_event, cancel_discord_event, get_guild_events and
send_channel_message with the signatures of services.discord_api. Tests pass
a fake; production passes the real module.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import (
    Delivery, AudienceDestination, DiscordServer, Event, EventAlliance, EventOccurrence, EventReminder, Tenant,
)
from services.destinations import (
    MERGED_PREFIX, Target, alliance_overrides, find_conflicts, group_sends, merge_key, plan_destinations, resolve_for_event,
)
from services.recurrence import build_start_datetime, event_dates_in_window
from services.templates import render_placeholders
from services.time_utils import ensure_utc

logger = logging.getLogger(__name__)

KIND_DISCORD_EVENT = "discord_event"
KIND_REMINDER = "reminder"
DISCORD_EVENT_MINUTES = -1

#: A Discord Scheduled Event is created this long before it starts.
DISCORD_EVENT_LEAD = timedelta(days=7)
#: Discord rejects a Scheduled Event that starts too soon, and a creation
#: notice that close to the start is pointless, so none is sent.
DISCORD_EVENT_FLOOR = timedelta(minutes=15)
#: A `sending` delivery older than this is declared lost (see module docstring).
STALE_CLAIM_AFTER = timedelta(minutes=10)
AT_START_GRACE = timedelta(minutes=5)


def _reminder_expired(minutes: int, start: datetime, now: datetime) -> bool:
    """An earlier reminder is pointless once the event has started. An "at the
    start" reminder (0) comes due at that very moment, so it gets a short grace
    period for the one-minute tick or a brief outage; before this it was always
    cancelled as "already started"."""
    return now - start > AT_START_GRACE if minutes == 0 else start <= now

MAX_CONTENT_CHARS = 2000
TICK_BATCH_SIZE = 200


def platform_bot_token() -> str:
    """Read at call time, not import time, so tests and a restarted service
    pick up the environment as it is now."""
    return os.environ.get("PLATFORM_BOT_TOKEN", "")


def effective_start(occ: EventOccurrence) -> datetime:
    return ensure_utc(occ.start_datetime_utc_override or occ.start_datetime_utc)


def effective_end(occ: EventOccurrence) -> datetime | None:
    """A moved occurrence keeps its duration."""
    if occ.end_datetime_utc is None:
        return None
    end = ensure_utc(occ.end_datetime_utc)
    if occ.start_datetime_utc_override is None:
        return end
    return effective_start(occ) + (end - ensure_utc(occ.start_datetime_utc))


@dataclass
class SyncResult:
    """What a sync changed that the caller may need to push to Discord."""
    created: int = 0
    updated: int = 0
    removed: int = 0
    #: Posted discord_event deliveries whose occurrence or audience went away.
    orphaned: list[Delivery] = field(default_factory=list)


async def audience_tenants(session: AsyncSession, event: Event) -> list[Tenant]:
    """Who the event goes to. An alliance event uses its EventAlliance rows
    (the owner alone if there are none); a kingdom-wide event reaches every
    alliance in the owner's Kingdom."""
    owner = await session.get(Tenant, event.owning_tenant_id)
    if event.scope == "kingdom-wide":
        result = await session.execute(select(Tenant).where(Tenant.kingdom_id == owner.kingdom_id).order_by(Tenant.id))
        return list(result.scalars().all())
    rows = await session.execute(select(EventAlliance.tenant_id).where(EventAlliance.event_id == event.id))
    tenant_ids = [r[0] for r in rows.all()]
    if not tenant_ids:
        return [owner]
    result = await session.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)).order_by(Tenant.id))
    return list(result.scalars().all())


@dataclass
class Wanted:
    """One delivery an occurrence needs (spec §67.2)."""
    kind: str
    minutes: int
    due: datetime
    tenant_id: int
    destination: AudienceDestination | None = None


def _row_key(kind: str, destination_id: int | None, tenant_id: int, minutes: int) -> tuple:
    """Identity of a delivery within an occurrence (spec §68.1): a reminder is
    per destination and alliance; a Discord event, or a reminder with no
    destination at all, is per alliance."""
    return (kind, destination_id, tenant_id, minutes)


async def event_overview(session: AsyncSession, event: Event) -> dict:
    """Spec §67.6: how many destinations and channels an event posts to, plus
    warnings (a message conflict, or nowhere to post). Computed live from the
    same functions the engine uses."""
    tenants = await audience_tenants(session, event)
    changes = {a.audience_id: a.included for a in event.audience_links}
    plan = await plan_destinations(
        session, tenants=tenants, owner=await session.get(Tenant, event.owning_tenant_id), changes=changes,
        leadership_only=event.leadership_only,
        base_message=event.message or "", overrides=alliance_overrides(event.alliances),
    )
    warnings = [c.message() for c in plan.conflicts]
    if event.active and event.reminders and not plan.resolution.targets:
        warnings.append(
            "No leadership-only audience is set up for this event." if event.leadership_only
            else "This event has no audience, so no reminders will be sent."
        )
    return {
        "audience_tenant_ids": [t.id for t in tenants],
        "audience_count": len(plan.resolution.audiences),
        "destination_count": len(plan.resolution.destinations),
        "channel_count": len(plan.sends),
        "warnings": warnings,
    }


def _wanted_deliveries(
    event: Event, occ: EventOccurrence, tenants: list[Tenant], targets: list[Target],
    reminder_minutes: list[int],
) -> dict[tuple, Wanted]:
    """Every delivery this occurrence needs, keyed by _row_key. Scheduled
    Events are per audience alliance (on its primary server); reminders are
    per resolved target (alliance and destination), or one placeholder per
    alliance when the event resolves to no destination so the log shows why
    nothing was sent."""
    start = effective_start(occ)
    wanted: dict[tuple, Wanted] = {}
    if event.duration_hours is not None and not event.leadership_only:
        for tenant in tenants:
            wanted[_row_key(KIND_DISCORD_EVENT, None, tenant.id, DISCORD_EVENT_MINUTES)] = Wanted(
                KIND_DISCORD_EVENT, DISCORD_EVENT_MINUTES, start - DISCORD_EVENT_LEAD, tenant.id)
    for minutes in reminder_minutes:
        due = start - timedelta(minutes=minutes)
        if targets:
            for target in targets:
                wanted[_row_key(KIND_REMINDER, target.id, target.tenant_id, minutes)] = Wanted(
                    KIND_REMINDER, minutes, due, target.tenant_id, target.destination)
        else:
            for tenant in tenants:
                wanted[_row_key(KIND_REMINDER, None, tenant.id, minutes)] = Wanted(
                    KIND_REMINDER, minutes, due, tenant.id)
    return wanted


def _new_delivery_state(kind: str, due: datetime, start: datetime, now: datetime) -> tuple[str, str | None]:
    """Initial status for a freshly generated delivery. A reminder whose time
    already passed is cancelled, not sent late: creating or editing an event
    must not fire notices for moments that are over. A Discord event inside
    the floor is cancelled for the same reason."""
    if kind == KIND_REMINDER and due <= now:
        return "cancelled", "Due time had already passed when scheduled"
    if kind == KIND_DISCORD_EVENT and start - now < DISCORD_EVENT_FLOOR:
        return "cancelled", "Starts within 15 minutes of scheduling"
    return "pending", None


async def sync_event_occurrences(
    session: AsyncSession, event: Event, now: datetime, window_days: int = 28,
) -> SyncResult:
    """Makes occurrences and deliveries match `event` for the window starting
    at now's UTC date. Flushes; the caller commits."""
    result = SyncResult()
    reminder_minutes = sorted({r.minutes_before for r in event.reminders}, reverse=True)
    today = now.astimezone(timezone.utc).date()

    occ_rows = await session.execute(
        select(EventOccurrence).where(EventOccurrence.event_id == event.id)
        .execution_options(populate_existing=True)
    )
    existing = {o.occurrence_date: o for o in occ_rows.scalars().all()}
    dates = set(event_dates_in_window(
        event.recurrence_kind, event.anchor_date, event.interval_days, event.until_date, today, window_days,
    )) if event.active else set()
    tenants = await audience_tenants(session, event) if event.active else []
    targets = (await resolve_for_event(session, event, tenants)).targets if event.active else []
    duration = timedelta(hours=float(event.duration_hours)) if event.duration_hours is not None else None

    for day in sorted(dates):
        start = build_start_datetime(day, event.start_time_utc)
        end = start + duration if duration is not None else None
        occ = existing.get(day)
        if occ is None:
            occ = EventOccurrence(
                event_id=event.id, occurrence_date=day, start_datetime_utc=start,
                end_datetime_utc=end, status="scheduled",
            )
            session.add(occ)
            await session.flush()
            result.created += 1
        elif occ.status == "cancelled":
            continue
        elif occ.start_datetime_utc_override is None:
            if ensure_utc(occ.start_datetime_utc) != start or (
                (ensure_utc(occ.end_datetime_utc) if occ.end_datetime_utc else None) != end
            ):
                occ.start_datetime_utc = start
                occ.end_datetime_utc = end
                result.updated += 1
        await _sync_occurrence_deliveries(session, event, occ, tenants, targets, reminder_minutes, now, result)

    # Occurrences the definition no longer produces (event deactivated, moved,
    # shortened by until_date, recurrence changed).
    for day, occ in existing.items():
        if day in dates or occ.status == "cancelled" or day < today or day >= today + timedelta(days=window_days):
            continue
        if occ.start_datetime_utc_override is not None and event.active:
            continue
        await _retire_occurrence(session, occ, result)
    await session.flush()
    return result


async def _sync_occurrence_deliveries(
    session: AsyncSession, event: Event, occ: EventOccurrence, tenants: list[Tenant],
    targets: list[Target], reminder_minutes: list[int], now: datetime, result: SyncResult,
) -> None:
    wanted = _wanted_deliveries(event, occ, tenants, targets, reminder_minutes)
    rows = await session.execute(
        select(Delivery).where(Delivery.occurrence_id == occ.id).execution_options(populate_existing=True)
    )
    current = {
        _row_key(d.kind, d.destination_id, d.tenant_id, d.reminder_minutes): d for d in rows.scalars().all()
    }
    start = effective_start(occ)

    for key, want in wanted.items():
        delivery = current.get(key)
        guild_id = want.destination.server.guild_id if want.destination else ""
        channel_id = want.destination.channel_id if want.destination else ""
        if delivery is None:
            status, detail = _new_delivery_state(want.kind, want.due, start, now)
            delivery = Delivery(
                occurrence_id=occ.id, tenant_id=want.tenant_id, kind=want.kind, reminder_minutes=want.minutes,
                destination_id=want.destination.id if want.destination else None,
                guild_id=guild_id, channel_id=channel_id, due_at_utc=want.due, status=status, detail=detail,
            )
            session.add(delivery)
            current[key] = delivery
        elif delivery.status in ("pending", "cancelled"):
            delivery.due_at_utc = want.due
            delivery.guild_id, delivery.channel_id = guild_id, channel_id
            status, detail = _new_delivery_state(want.kind, want.due, start, now)
            delivery.status, delivery.detail = status, detail
            delivery.merged_into_id = None
    await session.flush()

    for key, delivery in current.items():
        if key in wanted:
            continue
        if delivery.status == "pending":
            delivery.status, delivery.detail = "cancelled", "No longer part of the event"
            delivery.merged_into_id = None
        elif delivery.status == "posted" and delivery.kind == KIND_DISCORD_EVENT:
            result.orphaned.append(delivery)

    _apply_merges(wanted, current, start, now)
    await session.flush()


def _apply_merges(wanted: dict[tuple, Wanted], current: dict[tuple, Delivery], start: datetime, now: datetime) -> None:
    """Spec §67.3. Reminders to destinations that share (guild, channel) and an
    offset are one send: one delivery keeps the send and the rest are marked
    `cancelled`, linked through merged_into_id. Rows that already attempted a
    send are never rewritten."""
    groups: dict[tuple, list[tuple]] = {}
    for key, want in wanted.items():
        if want.kind != KIND_REMINDER or want.destination is None:
            continue
        groups.setdefault((want.minutes, *merge_key(want.destination)), []).append(key)

    for keys in groups.values():
        keys.sort(key=lambda k: (wanted[k].tenant_id, wanted[k].destination.id))
        rows = [current[k] for k in keys]
        attempted = [d for d in rows if d.status in ("sending", "posted", "error")]
        pending = [d for d in rows if d.status == "pending"]
        rep = (attempted or pending or rows)[0]
        if rep.status == "cancelled" and (rep.detail or "").startswith(MERGED_PREFIX):
            rep.status, rep.detail = _new_delivery_state(KIND_REMINDER, ensure_utc(rep.due_at_utc), start, now)
        rep.merged_into_id = None
        for d in rows:
            if d is rep:
                continue
            if d.status == "pending" or (d.status == "cancelled" and (d.detail or "").startswith(MERGED_PREFIX)):
                note = " (already sent)" if rep.status == "posted" else ""
                d.status, d.detail = "cancelled", f"{MERGED_PREFIX}{rep.id}{note}"
                d.merged_into_id = rep.id


async def _retire_occurrence(session: AsyncSession, occ: EventOccurrence, result: SyncResult) -> None:
    rows = await session.execute(
        select(Delivery).where(Delivery.occurrence_id == occ.id).execution_options(populate_existing=True)
    )
    deliveries = list(rows.scalars().all())
    if any(d.status in ("posted", "sending") for d in deliveries):
        # Something already went out: keep the history, cancel the rest.
        occ.status = "cancelled"
        for d in deliveries:
            if d.status == "pending":
                d.status, d.detail = "cancelled", "Occurrence no longer scheduled"
            elif d.status == "posted" and d.kind == KIND_DISCORD_EVENT:
                result.orphaned.append(d)
    else:
        await session.delete(occ)
    result.removed += 1


async def cancel_pending_deliveries(session: AsyncSession, occurrence_ids: list[int], detail: str) -> None:
    if not occurrence_ids:
        return
    await session.execute(
        update(Delivery)
        .where(Delivery.occurrence_id.in_(occurrence_ids), Delivery.status == "pending")
        .values(status="cancelled", detail=detail)
    )


# ---------------------------------------------------------------- Discord

def _guild_and_token(tenant: Tenant) -> tuple[str, str]:
    server: DiscordServer = tenant.server
    return server.guild_id, (server.bot_token or platform_bot_token())


async def _render(event: Event, occ: EventOccurrence, tenant: Tenant, text: str, now: datetime,
                  alliance_names: list[str] | None = None) -> str:
    """`alliance_names` replaces {alliance_name} when several alliances share
    one message (spec §67.3); it renders as a comma-separated list."""
    return render_placeholders(
        text, tenant_name=", ".join(alliance_names) if alliance_names else tenant.name,
        kingdom_name=tenant.kingdom.name, scheduled_for=now,
        event_offset_minutes=round((effective_start(occ) - now).total_seconds() / 60),
    )


async def _guild_alliance_names(session: AsyncSession, event: Event, guild_id: str) -> list[str]:
    """Names of the audience alliances whose primary server is this guild."""
    tenants = await audience_tenants(session, event)
    return sorted({t.name for t in tenants if t.server.guild_id == guild_id}, key=str.lower)


async def _effective_message(session: AsyncSession, event: Event, occ: EventOccurrence, tenant_id: int) -> str:
    """Occurrence override, then the alliance's override, then the event's."""
    if occ.message_override:
        return occ.message_override
    row = (await session.execute(
        select(EventAlliance).where(EventAlliance.event_id == event.id, EventAlliance.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if row is not None and row.message_override:
        return row.message_override
    return event.message or ""


async def _known_discord_ids(session: AsyncSession, guild_id: str) -> set[str]:
    rows = await session.execute(
        select(Delivery.discord_event_id)
        .join(Tenant, Tenant.id == Delivery.tenant_id)
        .join(DiscordServer, DiscordServer.id == Tenant.server_id)
        .where(DiscordServer.guild_id == guild_id, Delivery.discord_event_id.is_not(None))
    )
    return {r[0] for r in rows.all()}


async def _shared_guild_event(session: AsyncSession, occ: EventOccurrence, guild_id: str) -> tuple[str, int] | None:
    """Spec §52: alliances sharing a guild share one Discord event. Returns
    (discord event id, the delivery that created it)."""
    row = await session.execute(
        select(Delivery.discord_event_id, Delivery.id)
        .join(Tenant, Tenant.id == Delivery.tenant_id)
        .join(DiscordServer, DiscordServer.id == Tenant.server_id)
        .where(
            Delivery.occurrence_id == occ.id, Delivery.kind == KIND_DISCORD_EVENT,
            Delivery.status == "posted", Delivery.discord_event_id.is_not(None),
            DiscordServer.guild_id == guild_id,
        ).order_by(Delivery.id).limit(1)
    )
    found = row.first()
    return (found[0], found[1]) if found else None


def _parse_discord_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


async def _send_discord_event(
    session: AsyncSession, discord, delivery: Delivery, event: Event, occ: EventOccurrence,
    tenant: Tenant, now: datetime,
) -> tuple[str, str | None]:
    """Returns (status, detail) and sets delivery.discord_event_id."""
    guild_id, token = _guild_and_token(tenant)
    if not token:
        return "error", "No Discord bot token configured for this alliance"

    shared = await _shared_guild_event(session, occ, guild_id)
    if shared:
        delivery.discord_event_id, delivery.merged_into_id = shared
        return "posted", "Shared with another alliance in the same Discord server"

    start, end = effective_start(occ), effective_end(occ)
    known = await _known_discord_ids(session, guild_id)
    for existing in await discord.get_guild_events(token, guild_id):
        if str(existing.get("name", "")).strip().lower() != event.name.strip().lower():
            continue
        if str(existing.get("id")) in known:
            continue  # another occurrence of this same event
        existing_start = _parse_discord_time(existing.get("scheduled_start_time"))
        if existing_start is not None and abs((existing_start - start).total_seconds()) < 60:
            delivery.discord_event_id = str(existing["id"])
            return "posted", "Matched an existing Discord event with the same name and time"
        return "error", (
            f'A Discord event named "{event.name}" already exists at a different time. '
            "It was not changed. Remove it in Discord and retry."
        )

    message = await _effective_message(session, event, occ, tenant.id)
    discord_id, error = await discord.create_discord_event(
        token=token, guild_id=guild_id, name=event.name, start=start, end=end,
        description=await _render(
            event, occ, tenant, message, now, await _guild_alliance_names(session, event, guild_id),
        ) if message else "",
        location=event.location, image=event.cover_image_data or None,
    )
    if error:
        return "error", error
    delivery.discord_event_id = discord_id
    return "posted", None


async def _merged_targets(session: AsyncSession, delivery: Delivery) -> list[Target]:
    """The targets of one send: this delivery's own plus every delivery that
    was merged into it."""
    rows = await session.execute(select(Delivery).where(Delivery.merged_into_id == delivery.id))
    members = [delivery, *rows.scalars().all()]
    return [Target(m.tenant, m.destination) for m in members if m.destination is not None]


async def _reminder_message(session: AsyncSession, event: Event, minutes: int) -> str | None:
    row = (await session.execute(
        select(EventReminder).where(EventReminder.event_id == event.id, EventReminder.minutes_before == minutes)
    )).scalar_one_or_none()
    return row.message if row is not None and row.message else None


async def _send_reminder(
    session: AsyncSession, discord, delivery: Delivery, event: Event, occ: EventOccurrence,
    tenant: Tenant, now: datetime,
) -> tuple[str, str | None]:
    """One real message per (guild, channel): the representative delivery sends
    for every destination merged into it (spec §67.3, §67.4)."""
    dest = delivery.destination
    if dest is None:
        if event.leadership_only:
            return "error", "No leadership-only destination configured"
        return "error", "No destination configured for this alliance"
    if dest.audience.leadership_only != event.leadership_only:
        return "cancelled", "This audience does not take this kind of event"
    server = dest.server
    token = server.bot_token or platform_bot_token()
    if not token:
        return "error", "No Discord bot token configured for this server"

    members = await _merged_targets(session, delivery)
    sends = group_sends(members)
    send = sends[0]
    note: str | None = None
    reminder_message = await _reminder_message(session, event, delivery.reminder_minutes)
    if occ.message_override:
        message = occ.message_override
    elif reminder_message:
        message = reminder_message  # this reminder's own text beats alliance overrides (spec §77)
    else:
        overrides = alliance_overrides((await session.execute(
            select(EventAlliance).where(EventAlliance.event_id == event.id))).scalars().all())
        base = event.message or ""
        if find_conflicts(sends, base, overrides):
            message = base
            names = sorted({m.tenant.name for m in members if m.tenant_id in overrides})
            note = (f"Conflicting alliance messages on a shared channel; base message sent. "
                    f"Overrides for {', '.join(names)} were not used")
        else:
            message = overrides.get(delivery.tenant_id, base)
    message = message or f"{event.name} starts {{event_time_relative}}"
    content = await _render(event, occ, tenant, message, now, send.alliance_names)
    if event.mention_role and send.role_ids:
        content = " ".join(f"<@&{r}>" for r in send.role_ids) + f" {content}"
    ok, error = await discord.send_channel_message(token, dest.channel_id, content[:MAX_CONTENT_CHARS])
    if not ok:
        return "error", error
    return "posted", note


# ------------------------------------------------------------------- tick

async def _claim(session: AsyncSession, delivery_id: int, now: datetime) -> bool:
    result = await session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.status == "pending")
        .values(status="sending", claimed_at_utc=now)
    )
    await session.commit()
    return result.rowcount == 1


async def _finish(session_factory, delivery_id: int, status: str, detail: str | None,
                  discord_event_id: str | None, now: datetime, merged_into_id: int | None = None) -> None:
    async with session_factory() as session:
        values = {"status": status, "detail": detail}
        if merged_into_id is not None:
            values["merged_into_id"] = merged_into_id
        if status == "posted":
            values["posted_at_utc"] = now
        if discord_event_id:
            values["discord_event_id"] = discord_event_id
        await session.execute(update(Delivery).where(Delivery.id == delivery_id).values(**values))
        await session.commit()


async def process_delivery(session_factory, discord, delivery_id: int, now: datetime) -> str | None:
    """Claims and sends one delivery. Returns the final status, or None when
    another worker already claimed it. Never raises."""
    async with session_factory() as session:
        if not await _claim(session, delivery_id, now):
            return None

    status, detail, discord_event_id, merged_into_id = "error", "Unexpected failure before sending", None, None
    try:
        async with session_factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            occ = await session.get(EventOccurrence, delivery.occurrence_id)
            event = await session.get(Event, occ.event_id)
            tenant = await session.get(Tenant, delivery.tenant_id)
            start = effective_start(occ)

            if not event.active or occ.status == "cancelled":
                status, detail = "cancelled", "Event or occurrence no longer active"
            elif delivery.kind == KIND_REMINDER and _reminder_expired(delivery.reminder_minutes, start, now):
                status, detail = "cancelled", "Event had already started"
            elif delivery.kind == KIND_DISCORD_EVENT and start - now < DISCORD_EVENT_FLOOR:
                status, detail = "cancelled", "Starts within 15 minutes; no Discord event created"
            elif delivery.kind == KIND_DISCORD_EVENT:
                status, detail = await _send_discord_event(session, discord, delivery, event, occ, tenant, now)
                discord_event_id, merged_into_id = delivery.discord_event_id, delivery.merged_into_id
            else:
                status, detail = await _send_reminder(session, discord, delivery, event, occ, tenant, now)
    except Exception as exc:  # one delivery's failure must not stop the tick
        logger.exception("delivery %s failed", delivery_id)
        status, detail = "error", f"{type(exc).__name__}: {exc}"
    await _finish(session_factory, delivery_id, status, detail, discord_event_id, now, merged_into_id)
    return status


async def recover_stale_claims(session_factory, now: datetime) -> int:
    async with session_factory() as session:
        result = await session.execute(
            update(Delivery)
            .where(Delivery.status == "sending", Delivery.claimed_at_utc < now - STALE_CLAIM_AFTER)
            .values(status="error", detail="Interrupted while sending; it may or may not have been delivered. "
                                           "Check Discord before retrying.")
        )
        await session.commit()
        return result.rowcount or 0


async def run_delivery_tick(session_factory, discord, now: datetime | None = None) -> dict[str, int]:
    """The single per-minute job. Returns counts by final status."""
    now = now or datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    stale = await recover_stale_claims(session_factory, now)
    if stale:
        counts["stale"] = stale
    async with session_factory() as session:
        rows = await session.execute(
            select(Delivery.id).where(Delivery.status == "pending", Delivery.due_at_utc <= now)
            .order_by(Delivery.due_at_utc, Delivery.id).limit(TICK_BATCH_SIZE)
        )
        ids = [r[0] for r in rows.all()]
    for delivery_id in ids:
        status = await process_delivery(session_factory, discord, delivery_id, now)
        if status:
            counts[status] = counts.get(status, 0) + 1
    return counts


async def resync_kingdom_events(session: AsyncSession, kingdom_id: int, now: datetime | None = None) -> int:
    """Regenerates the deliveries of every active event in a Kingdom. Called
    after an Audience changes (spec §67.3), so pending
    deliveries follow the new setup. Flushes; the caller commits."""
    now = now or datetime.now(timezone.utc)
    rows = await session.execute(
        select(Event).join(Tenant, Tenant.id == Event.owning_tenant_id)
        .where(Tenant.kingdom_id == kingdom_id, Event.active.is_(True))
        .execution_options(populate_existing=True)
    )
    events = list(rows.scalars().unique().all())
    for event in events:
        await session.refresh(event, ["reminders", "alliances", "audience_links"])
        await sync_event_occurrences(session, event, now)
    return len(events)


async def run_generation(session_factory, now: datetime | None = None) -> int:
    """Daily job (and startup catch-up): sync every active event. One event's
    failure does not stop the others. Returns how many events synced."""
    now = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        ids = [r[0] for r in (await session.execute(select(Event.id).where(Event.active.is_(True)))).all()]
    synced = 0
    for event_id in ids:
        try:
            async with session_factory() as session:
                event = (await session.execute(
                    select(Event).where(Event.id == event_id).execution_options(populate_existing=True)
                )).scalar_one()
                await session.refresh(event, ["reminders"])
                await sync_event_occurrences(session, event, now)
                await session.commit()
            synced += 1
        except Exception:
            logger.exception("generation failed for event %s", event_id)
    return synced


# ----------------------------------------------------- posted Discord events

def _unique_posted(deliveries: list[Delivery], guild_of: dict[int, tuple[str, str]]):
    """One entry per (guild, discord event), since alliances sharing a guild
    share the Discord event."""
    seen: dict[tuple[str, str], list[Delivery]] = {}
    for d in deliveries:
        if d.kind != KIND_DISCORD_EVENT or d.status != "posted" or not d.discord_event_id:
            continue
        guild_id, _ = guild_of[d.tenant_id]
        seen.setdefault((guild_id, d.discord_event_id), []).append(d)
    return seen


async def _guild_map(session: AsyncSession, deliveries: list[Delivery]) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for tenant_id in {d.tenant_id for d in deliveries}:
        tenant = await session.get(Tenant, tenant_id)
        out[tenant_id] = _guild_and_token(tenant)
    return out


async def remove_posted_discord_events(session: AsyncSession, discord, deliveries: list[Delivery]) -> list[str]:
    """Deletes the Discord Scheduled Events behind posted deliveries and marks
    the rows cancelled. Best effort: a failure is returned and left on the
    row, and the rest still run."""
    errors: list[str] = []
    guild_of = await _guild_map(session, deliveries)
    for (guild_id, discord_id), group in _unique_posted(deliveries, guild_of).items():
        _, token = guild_of[group[0].tenant_id]
        ok, error = await discord.cancel_discord_event(token, guild_id, discord_id)
        for d in group:
            if ok:
                d.status, d.detail = "cancelled", "Discord event removed"
            else:
                d.detail = f"Could not remove the Discord event: {error}"
        if not ok:
            errors.append(error)
    await session.flush()
    return errors


async def refresh_posted_discord_events(
    session: AsyncSession, discord, event: Event, occurrences: list[EventOccurrence], now: datetime,
) -> list[str]:
    """Pushes the current definition (name, description, location, cover,
    times) to every posted Discord event of the given occurrences."""
    errors: list[str] = []
    for occ in occurrences:
        rows = await session.execute(select(Delivery).where(
            Delivery.occurrence_id == occ.id, Delivery.kind == KIND_DISCORD_EVENT, Delivery.status == "posted",
        ))
        deliveries = list(rows.scalars().all())
        guild_of = await _guild_map(session, deliveries)
        for (guild_id, discord_id), group in _unique_posted(deliveries, guild_of).items():
            tenant = await session.get(Tenant, group[0].tenant_id)
            _, token = guild_of[tenant.id]
            message = await _effective_message(session, event, occ, tenant.id)
            ok, error = await discord.update_discord_event(
                token, guild_id, discord_id, name=event.name,
                description=await _render(
                    event, occ, tenant, message, now, await _guild_alliance_names(session, event, guild_id),
                ) if message else "",
                location=event.location, image=event.cover_image_data or None,
                start=effective_start(occ), end=effective_end(occ),
            )
            if not ok:
                errors.append(error)
                for d in group:
                    d.detail = f"Could not update the Discord event: {error}"
    await session.flush()
    return errors


async def apply_event_change(session: AsyncSession, discord, event: Event, now: datetime) -> list[str]:
    """After any change to an event definition ("all occurrences" scope):
    regenerate occurrences and deliveries, remove Discord events that no
    longer belong, and push the new definition to the ones that do. Returns
    Discord errors for the caller to surface; the database change itself has
    already succeeded."""
    result = await sync_event_occurrences(session, event, now)
    errors = await remove_posted_discord_events(session, discord, result.orphaned)
    rows = await session.execute(
        select(EventOccurrence).where(
            EventOccurrence.event_id == event.id, EventOccurrence.status == "scheduled",
            EventOccurrence.occurrence_date >= today_utc(now),
        )
    )
    errors += await refresh_posted_discord_events(session, discord, event, list(rows.scalars().all()), now)
    return errors


async def remove_event_from_discord(session: AsyncSession, discord, event_id: int) -> list[str]:
    """Used before deleting an event: removes every Discord Scheduled Event it
    created. Returns Discord errors."""
    rows = await session.execute(
        select(Delivery).join(EventOccurrence, EventOccurrence.id == Delivery.occurrence_id)
        .where(EventOccurrence.event_id == event_id, Delivery.kind == KIND_DISCORD_EVENT,
               Delivery.status == "posted")
    )
    return await remove_posted_discord_events(session, discord, list(rows.scalars().all()))


def today_utc(now: datetime) -> date:
    return now.astimezone(timezone.utc).date()
