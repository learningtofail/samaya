"""Shared helpers for the unified-event-model tests (spec §66): a fake
Discord client and direct-to-database builders. Fixtures live in conftest.py."""
from datetime import date, datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from models.db import (
    AllianceAudience, Audience, AudienceDestination, Delivery, Event, EventAlliance, EventAudience, EventOccurrence, EventReminder, EventType, Tenant,
)
from services.event_engine import sync_event_occurrences

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class FakeDiscord:
    """Records every call and keeps a tiny in-memory guild calendar."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.guild_events: dict[str, list[dict]] = {}
        self.create_error = ""
        self.send_error = ""
        self.cancel_error = ""
        self.raise_on_send_to: set[str] = set()
        self._next = 0

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    async def create_discord_event(self, token, guild_id, name, start, end, description, location, image=None):
        self.calls.append(("create", guild_id, name, start, end, description))
        if self.create_error:
            return "", self.create_error
        self._next += 1
        discord_id = f"de{self._next}"
        self.guild_events.setdefault(guild_id, []).append(
            {"id": discord_id, "name": name, "scheduled_start_time": start.isoformat()})
        return discord_id, ""

    async def update_discord_event(self, token, guild_id, discord_event_id, name, description, location,
                                   image=None, start=None, end=None):
        self.calls.append(("update", guild_id, discord_event_id, name, description, start, end))
        return True, ""

    async def cancel_discord_event(self, token, guild_id, discord_event_id):
        self.calls.append(("cancel", guild_id, discord_event_id))
        if self.cancel_error:
            return False, self.cancel_error
        self.guild_events[guild_id] = [e for e in self.guild_events.get(guild_id, []) if e["id"] != discord_event_id]
        return True, ""

    async def get_guild_events(self, token, guild_id):
        return list(self.guild_events.get(guild_id, []))

    async def send_channel_message(self, token, channel_id, content):
        self.calls.append(("send", channel_id, content))
        if channel_id in self.raise_on_send_to:
            raise RuntimeError("boom")
        return (False, self.send_error) if self.send_error else (True, "")


async def _type_id(s, kingdom_id: int) -> int:
    row = (await s.execute(select(EventType).where(EventType.kingdom_id == kingdom_id))).scalars().first()
    if row is None:
        row = EventType(kingdom_id=kingdom_id, name="General")
        s.add(row)
        await s.flush()
    return row.id


async def make_event(sf, tenant, *, name="Bear Hunt", anchor=date(2026, 10, 2), start="19:00", duration=2.0,
                     interval=7, reminders=(60,), scope="alliance", leadership_only=False, message="",
                     mention_role=False, audience=None, active=True) -> int:
    async with sf() as s:
        hour, minute = map(int, start.split(":"))
        event = Event(
            owning_tenant_id=tenant["id"], type_id=await _type_id(s, tenant["kingdom_id"]), name=name,
            scope=scope, leadership_only=leadership_only, message=message, start_time_utc=time(hour, minute),
            duration_hours=duration, recurrence_kind="interval_days" if interval else "none",
            interval_days=interval, anchor_date=anchor, mention_role=mention_role, active=active,
        )
        event.reminders = [EventReminder(minutes_before=m) for m in reminders]
        event.alliances = [EventAlliance(tenant_id=t) for t in (audience or [tenant["id"]])]
        s.add(event)
        await s.commit()
        return event.id


async def sync(sf, event_id, now=NOW):
    async with sf() as s:
        event = (await s.execute(
            select(Event).options(selectinload(Event.reminders), selectinload(Event.alliances))
            .where(Event.id == event_id).execution_options(populate_existing=True)
        )).scalar_one()
        result = await sync_event_occurrences(s, event, now)
        await s.commit()
        return result


async def deliveries(sf, event_id=None, **filters):
    async with sf() as s:
        stmt = select(Delivery).join(EventOccurrence, EventOccurrence.id == Delivery.occurrence_id)
        if event_id is not None:
            stmt = stmt.where(EventOccurrence.event_id == event_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Delivery, key) == value)
        return list((await s.execute(stmt.order_by(Delivery.due_at_utc, Delivery.id))).scalars().all())


async def add_audience(sf, tenant, channel, role="", *, label=None, default=True,
                       leadership=False, server_id=None, tenant_id=None, link=True, also=()) -> int:
    """An Audience in `tenant`'s Kingdom (a fixture dict) posting to `channel` on the tenant's primary server
    unless told otherwise, linked to the tenant (`tenant_id` overrides) with the given default.
    `also` adds more destinations as (server_id, channel, role) tuples. Returns the Audience id."""
    from models.db import Tenant
    async with sf() as s:
        owner = await s.get(Tenant, tenant_id or tenant["id"])
        audience = Audience(kingdom_id=owner.kingdom_id, label=label or f"Audience {channel} {owner.id}", leadership_only=leadership)
        audience.destinations = [
            AudienceDestination(server_id=server_id or tenant["server_id"], channel_id=channel, role_id=role),
            *[AudienceDestination(server_id=sid, channel_id=ch, role_id=rl) for sid, ch, rl in also],
        ]
        s.add(audience)
        await s.flush()
        if link:
            s.add(AllianceAudience(tenant_id=owner.id, audience_id=audience.id, post_by_default=default))
        await s.commit()
        return audience.id


async def link_audience(sf, audience_id, tenant_id, default=True) -> None:
    async with sf() as s:
        s.add(AllianceAudience(tenant_id=tenant_id, audience_id=audience_id, post_by_default=default))
        await s.commit()


async def change_audience(sf, event_id, audience_id, included: bool) -> None:
    async with sf() as s:
        s.add(EventAudience(event_id=event_id, audience_id=audience_id, included=included))
        await s.commit()


async def tenant_row(sf, tenant_id):
    async with sf() as s:
        return await s.get(Tenant, tenant_id)
