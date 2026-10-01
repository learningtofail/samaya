"""Unified event model, Phase 2 (spec §66.4, §66.4a): the delivery engine and
its routes, tested against a fake Discord client. Nothing here calls Discord.

Engine-level tests pass an explicit `now` so they do not depend on the real
clock. Route tests build dates relative to the real today and pass matching
`now` values to the tick.
"""
import asyncio
from datetime import date, datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from models.db import (
    Delivery, DiscordServer, Event, EventAlliance, EventOccurrence, EventReminder, EventType, Tenant,
)
from services.discord_client import get_discord
from services.event_engine import (
    STALE_CLAIM_AFTER, process_delivery, run_delivery_tick, run_generation, sync_event_occurrences,
)

BASE = "/admin/api/v2"
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


@pytest.fixture
def fake():
    return FakeDiscord()


@pytest.fixture
def sf(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def configured(sf, tenant):
    """The default tenant with a notification channel and role."""
    async with sf() as s:
        t = await s.get(Tenant, tenant["id"])
        t.notification_channel_id, t.notification_role_id = "chan-mod", "role-mod"
        await s.commit()
    return tenant


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


# ----------------------------------------------------------------- generation

class TestGeneration:
    async def test_window_and_delivery_shape(self, sf, tenant):
        event_id = await make_event(sf, tenant)
        result = await sync(sf, event_id)
        assert result.created == 4  # Oct 2, 9, 16, 23
        rows = await deliveries(sf, event_id)
        kinds = sorted((d.kind, d.reminder_minutes) for d in rows)
        assert kinds == [("discord_event", -1)] * 4 + [("reminder", 60)] * 4

    async def test_sync_is_idempotent(self, sf, tenant):
        event_id = await make_event(sf, tenant)
        await sync(sf, event_id)
        first = len(await deliveries(sf, event_id))
        result = await sync(sf, event_id)
        assert (result.created, result.removed) == (0, 0)
        assert len(await deliveries(sf, event_id)) == first

    async def test_no_duration_means_no_discord_event(self, sf, tenant):
        event_id = await make_event(sf, tenant, duration=None)
        await sync(sf, event_id)
        assert {d.kind for d in await deliveries(sf, event_id)} == {"reminder"}

    async def test_leadership_event_never_gets_a_discord_event(self, sf, tenant):
        event_id = await make_event(sf, tenant, leadership_only=True)
        await sync(sf, event_id)
        assert {d.kind for d in await deliveries(sf, event_id)} == {"reminder"}

    async def test_past_due_reminder_is_cancelled_not_sent_late(self, sf, tenant):
        # Starts at 12:30 today; the 60 minute reminder was due at 11:30.
        event_id = await make_event(sf, tenant, anchor=NOW.date(), start="12:30", interval=None)
        await sync(sf, event_id)
        reminder = (await deliveries(sf, event_id, kind="reminder"))[0]
        assert reminder.status == "cancelled"
        assert "already passed" in reminder.detail

    async def test_discord_event_inside_the_floor_is_cancelled(self, sf, tenant):
        event_id = await make_event(sf, tenant, anchor=NOW.date(), start="12:10", interval=None)
        await sync(sf, event_id)
        assert (await deliveries(sf, event_id, kind="discord_event"))[0].status == "cancelled"

    async def test_kingdom_wide_reaches_every_alliance_in_the_kingdom(self, sf, tenant, second_tenant):
        event_id = await make_event(sf, tenant, scope="kingdom-wide", interval=None)
        await sync(sf, event_id)
        assert {d.tenant_id for d in await deliveries(sf, event_id)} == {tenant["id"], second_tenant["id"]}

    async def test_subset_audience(self, sf, tenant, second_tenant):
        event_id = await make_event(sf, tenant, interval=None, audience=[second_tenant["id"]])
        await sync(sf, event_id)
        assert {d.tenant_id for d in await deliveries(sf, event_id)} == {second_tenant["id"]}

    async def test_deactivating_removes_unposted_occurrences(self, sf, tenant):
        event_id = await make_event(sf, tenant)
        await sync(sf, event_id)
        async with sf() as s:
            (await s.get(Event, event_id)).active = False
            await s.commit()
        await sync(sf, event_id)
        assert await deliveries(sf, event_id) == []

    async def test_shortening_until_date_removes_later_occurrences(self, sf, tenant):
        event_id = await make_event(sf, tenant)
        await sync(sf, event_id)
        async with sf() as s:
            (await s.get(Event, event_id)).until_date = date(2026, 10, 10)
            await s.commit()
        await sync(sf, event_id)
        async with sf() as s:
            dates = (await s.execute(select(EventOccurrence.occurrence_date).where(
                EventOccurrence.event_id == event_id).order_by(EventOccurrence.occurrence_date))).scalars().all()
        assert dates == [date(2026, 10, 2), date(2026, 10, 9)]

    async def test_changing_start_time_moves_pending_deliveries(self, sf, tenant):
        event_id = await make_event(sf, tenant)
        await sync(sf, event_id)
        async with sf() as s:
            (await s.get(Event, event_id)).start_time_utc = time(20, 0)
            await s.commit()
        await sync(sf, event_id)
        reminder = (await deliveries(sf, event_id, kind="reminder"))[0]
        assert reminder.due_at_utc.replace(tzinfo=UTC) == datetime(2026, 10, 2, 19, 0, tzinfo=UTC)

    async def test_run_generation_syncs_active_events(self, sf, tenant):
        await make_event(sf, tenant)
        await make_event(sf, tenant, name="Off", active=False)
        assert await run_generation(sf, NOW) == 1
        assert len(await deliveries(sf)) == 8


# -------------------------------------------------------------------- sending

class TestReminders:
    async def test_reminder_goes_to_the_alliance_channel_with_role_mention(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="Bear at {event_time_relative}", mention_role=True)
        await sync(sf, event_id)
        counts = await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        assert counts["posted"] >= 1
        _, channel, content = next(c for c in fake.calls if c[0] == "send")
        assert channel == "chan-mod"
        assert content.startswith("<@&role-mod> Bear at <t:")

    async def test_role_not_mentioned_unless_the_event_asks(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="Hi")
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        assert next(c for c in fake.calls if c[0] == "send")[2] == "Hi"

    async def test_empty_message_falls_back_to_name_and_time(self, sf, fake, configured):
        event_id = await make_event(sf, configured)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        assert next(c for c in fake.calls if c[0] == "send")[2].startswith("Bear Hunt starts <t:")

    async def test_message_precedence(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", interval=None)
        async with sf() as s:
            row = (await s.execute(select(EventAlliance).where(EventAlliance.event_id == event_id))).scalar_one()
            row.message_override = "alliance message"
            await s.commit()
        await sync(sf, event_id)
        at = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)
        await run_delivery_tick(sf, fake, at)
        assert next(c for c in fake.calls if c[0] == "send")[2] == "alliance message"

        fake.calls.clear()
        async with sf() as s:
            occ = (await s.execute(select(EventOccurrence).where(EventOccurrence.event_id == event_id))).scalar_one()
            occ.message_override = "occurrence message"
            d = (await s.execute(select(Delivery).where(Delivery.kind == "reminder"))).scalar_one()
            d.status = "pending"
            await s.commit()
        await run_delivery_tick(sf, fake, at)
        assert next(c for c in fake.calls if c[0] == "send")[2] == "occurrence message"

    async def test_alliance_destination_override(self, sf, fake, configured):
        event_id = await make_event(sf, configured, mention_role=True)
        async with sf() as s:
            row = (await s.execute(select(EventAlliance).where(EventAlliance.event_id == event_id))).scalar_one()
            row.notification_channel_id, row.notification_role_id = "leaders", "leader-role"
            await s.commit()
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        _, channel, content = next(c for c in fake.calls if c[0] == "send")
        assert channel == "leaders" and "<@&leader-role>" in content

    async def test_missing_channel_is_an_error_not_a_silent_skip(self, sf, fake, tenant):
        event_id = await make_event(sf, tenant)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        reminder = (await deliveries(sf, event_id, kind="reminder"))[0]
        assert reminder.status == "error" and "notification channel" in reminder.detail
        assert fake.count("send") == 0

    async def test_late_reminder_still_sends_before_the_event_starts(self, sf, fake, configured):
        event_id = await make_event(sf, configured)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 40, tzinfo=UTC))  # 40 min late, 20 to start
        assert fake.count("send") == 1

    async def test_reminder_is_cancelled_once_the_event_has_started(self, sf, fake, configured):
        event_id = await make_event(sf, configured)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 19, 5, tzinfo=UTC))
        reminder = (await deliveries(sf, event_id, kind="reminder"))[0]
        assert reminder.status == "cancelled" and fake.count("send") == 0

    async def test_send_failure_is_recorded(self, sf, fake, configured):
        fake.send_error = "Missing Access"
        event_id = await make_event(sf, configured)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        reminder = (await deliveries(sf, event_id, kind="reminder"))[0]
        assert (reminder.status, reminder.detail) == ("error", "Missing Access")


class TestTickSafety:
    async def test_a_posted_delivery_is_never_sent_twice(self, sf, fake, configured):
        event_id = await make_event(sf, configured)
        await sync(sf, event_id)
        at = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)
        await run_delivery_tick(sf, fake, at)
        sends = fake.count("send")
        await run_delivery_tick(sf, fake, at)
        await run_delivery_tick(sf, fake, at + timedelta(minutes=1))
        assert fake.count("send") == sends == 1

    async def test_two_workers_racing_for_one_delivery_send_once(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None, duration=None)
        await sync(sf, event_id)
        reminder = (await deliveries(sf, event_id))[0]
        at = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)
        results = await asyncio.gather(
            process_delivery(sf, fake, reminder.id, at), process_delivery(sf, fake, reminder.id, at))
        assert sorted(r or "" for r in results) == ["", "posted"]
        assert fake.count("send") == 1

    async def test_one_failing_delivery_does_not_block_the_rest(self, sf, fake, configured, second_tenant):
        async with sf() as s:
            t = await s.get(Tenant, second_tenant["id"])
            t.notification_channel_id = "chan-nsr"
            await s.commit()
        fake.raise_on_send_to = {"chan-mod"}
        event_id = await make_event(sf, configured, scope="kingdom-wide", interval=None, duration=None)
        await sync(sf, event_id)
        counts = await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        assert counts == {"error": 1, "posted": 1}
        failed = (await deliveries(sf, event_id, tenant_id=configured["id"]))[0]
        assert failed.status == "error" and "boom" in failed.detail

    async def test_stale_claim_becomes_an_error_and_is_not_retried(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None, duration=None)
        await sync(sf, event_id)
        at = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)
        async with sf() as s:
            d = (await s.execute(select(Delivery))).scalar_one()
            d.status, d.claimed_at_utc = "sending", at - STALE_CLAIM_AFTER - timedelta(minutes=1)
            await s.commit()
        counts = await run_delivery_tick(sf, fake, at)
        assert counts == {"stale": 1}
        row = (await deliveries(sf, event_id))[0]
        assert row.status == "error" and "may or may not" in row.detail
        assert fake.count("send") == 0

    async def test_fresh_claim_is_left_alone(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None, duration=None)
        await sync(sf, event_id)
        at = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)
        async with sf() as s:
            d = (await s.execute(select(Delivery))).scalar_one()
            d.status, d.claimed_at_utc = "sending", at - timedelta(minutes=1)
            await s.commit()
        assert await run_delivery_tick(sf, fake, at) == {}
        assert (await deliveries(sf, event_id))[0].status == "sending"

    async def test_cancelled_or_inactive_work_is_not_sent(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None, duration=None)
        await sync(sf, event_id)
        async with sf() as s:
            (await s.get(Event, event_id)).active = False
            await s.commit()
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        assert fake.count("send") == 0
        assert (await deliveries(sf, event_id))[0].status == "cancelled"


class TestDiscordEvents:
    AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    async def test_creates_the_discord_event_with_rendered_description(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="Starts {event_time_relative} for {alliance_name}",
                                    interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, self.AT)
        _, guild, name, start, end, description = next(c for c in fake.calls if c[0] == "create")
        assert (guild, name) == ("test-guild-mod", "Bear Hunt")
        assert start == datetime(2026, 10, 2, 19, 0, tzinfo=UTC) and end - start == timedelta(hours=2)
        assert description.startswith("Starts <t:") and description.endswith("for MOD")
        row = (await deliveries(sf, event_id, kind="discord_event"))[0]
        assert (row.status, row.discord_event_id) == ("posted", "de1")

    async def test_failure_is_recorded_and_retry_succeeds(self, sf, fake, configured):
        fake.create_error = "50013 Missing Permissions"
        event_id = await make_event(sf, configured, interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, self.AT)
        row = (await deliveries(sf, event_id, kind="discord_event"))[0]
        assert (row.status, row.detail) == ("error", "50013 Missing Permissions")
        fake.create_error = ""
        async with sf() as s:
            (await s.get(Delivery, row.id)).status = "pending"
            await s.commit()
        await run_delivery_tick(sf, fake, self.AT)
        assert (await deliveries(sf, event_id, kind="discord_event"))[0].status == "posted"

    async def test_starting_within_fifteen_minutes_creates_nothing(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 50, tzinfo=UTC))
        row = (await deliveries(sf, event_id, kind="discord_event"))[0]
        assert row.status == "cancelled" and fake.count("create") == 0

    async def test_existing_event_with_same_name_and_time_is_adopted(self, sf, fake, configured):
        fake.guild_events["test-guild-mod"] = [
            {"id": "manual1", "name": "bear hunt", "scheduled_start_time": "2026-10-02T19:00:00+00:00"}]
        event_id = await make_event(sf, configured, interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, self.AT)
        row = (await deliveries(sf, event_id, kind="discord_event"))[0]
        assert (row.status, row.discord_event_id) == ("posted", "manual1")
        assert fake.count("create") == 0

    async def test_same_name_at_a_different_time_is_an_error_and_untouched(self, sf, fake, configured):
        fake.guild_events["test-guild-mod"] = [
            {"id": "manual1", "name": "Bear Hunt", "scheduled_start_time": "2026-10-02T21:00:00+00:00"}]
        event_id = await make_event(sf, configured, interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, self.AT)
        row = (await deliveries(sf, event_id, kind="discord_event"))[0]
        assert row.status == "error" and "different time" in row.detail
        assert fake.count("create") == 0 and fake.count("update") == 0 and fake.count("cancel") == 0

    async def test_other_occurrences_of_the_same_event_are_not_conflicts(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=1, reminders=())
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 0, 0, tzinfo=UTC))
        assert (await deliveries(sf, event_id, kind="discord_event", status="error")) == []
        assert fake.count("create") > 3

    async def test_alliances_sharing_a_guild_share_one_discord_event(self, sf, fake, configured, db_engine):
        async with sf() as s:
            sibling = Tenant(kingdom_id=configured["kingdom_id"], server_id=configured["server_id"],
                             name="Sibling", slug="sibling")
            s.add(sibling)
            await s.commit()
        event_id = await make_event(sf, configured, scope="kingdom-wide", interval=None, reminders=())
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, self.AT)
        rows = await deliveries(sf, event_id, kind="discord_event")
        assert fake.count("create") == 1
        assert [r.status for r in rows] == ["posted", "posted"]
        assert len({r.discord_event_id for r in rows}) == 1

    async def test_missing_token_is_an_error(self, sf, fake, configured, monkeypatch):
        async with sf() as s:
            (await s.get(DiscordServer, configured["server_id"])).bot_token = None
            await s.commit()
        monkeypatch.delenv("PLATFORM_BOT_TOKEN", raising=False)
        event_id = await make_event(sf, configured, interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, self.AT)
        assert "bot token" in (await deliveries(sf, event_id, kind="discord_event"))[0].detail


# --------------------------------------------------------------------- routes

def _future(days: int) -> date:
    return datetime.now(UTC).date() + timedelta(days=days)


@pytest.fixture
def fake_discord(client, fake):
    from main import app
    app.dependency_overrides[get_discord] = lambda: fake
    return fake


async def _create_event(client, **overrides):
    types = (await client.get(f"{BASE}/event-types")).json()
    if not types:
        types = [(await client.post(f"{BASE}/event-types", json={"name": "General"})).json()]
    body = {"type_id": types[0]["id"], "name": "Bear Hunt", "start_time_utc": "19:00",
            "anchor_date": str(_future(2)), "duration_hours": 2, "recurrence_kind": "interval_days",
            "interval_days": 7, "reminder_minutes": [60]}
    body.update(overrides)
    resp = await client.post(f"{BASE}/events", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _occurrences(client):
    return (await client.get(f"{BASE}/occurrences")).json()


async def _post_everything(sf, fake):
    """Plays the clock forward so every occurrence in the window gets its
    Discord event, as the per-minute tick would: each occurrence is posted
    7 days ahead, before it starts."""
    now = datetime.now(UTC)
    for days in (1, 3, 10, 17):
        await run_delivery_tick(sf, fake, now + timedelta(days=days))


class TestCreateGeneratesWork:
    async def test_creating_an_event_creates_occurrences(self, client, fake_discord):
        await _create_event(client)
        occs = await _occurrences(client)
        assert len(occs) == 4 and all(o["status"] == "scheduled" for o in occs)
        assert occs[0]["delivery_counts"] == {"pending": 2}


class TestOccurrenceEdits:
    async def test_cancel_one_occurrence_removes_its_discord_event(self, client, fake_discord, sf):
        await _create_event(client)
        await _post_everything(sf, fake_discord)
        occ = (await _occurrences(client))[0]
        resp = await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"cancelled": True})
        assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
        assert fake_discord.count("cancel") == 1
        rows = await deliveries(sf)
        mine = [d for d in rows if d.occurrence_id == occ["id"]]
        assert {d.status for d in mine} == {"cancelled"}
        # the next occurrence is untouched
        assert (await _occurrences(client))[1]["status"] == "scheduled"

    async def test_cancelled_occurrence_survives_regeneration(self, client, fake_discord, sf):
        await _create_event(client)
        occ = (await _occurrences(client))[0]
        await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"cancelled": True})
        await run_generation(sf, datetime.now(UTC))
        assert (await _occurrences(client))[0]["status"] == "cancelled"

    async def test_restoring_recreates_the_discord_event(self, client, fake_discord, sf):
        await _create_event(client)
        await _post_everything(sf, fake_discord)
        occ = (await _occurrences(client))[0]
        await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"cancelled": True})
        resp = await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"cancelled": False})
        assert resp.json()["status"] == "scheduled"
        await _post_everything(sf, fake_discord)
        assert fake_discord.count("create") == 5  # 4 originally + 1 recreated

    async def test_move_one_occurrence_updates_discord_and_deliveries(self, client, fake_discord, sf):
        await _create_event(client)
        await _post_everything(sf, fake_discord)
        occ = (await _occurrences(client))[0]
        new_start = datetime.combine(_future(2), time(21, 0), tzinfo=UTC)
        resp = await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"start_datetime_utc": new_start.isoformat()})
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_moved"] is True
        assert resp.json()["end_datetime_utc"] == (new_start + timedelta(hours=2)).isoformat()
        update = next(c for c in fake_discord.calls if c[0] == "update")
        assert update[5] == new_start and update[6] == new_start + timedelta(hours=2)
        reminder = next(d for d in await deliveries(sf, kind="reminder") if d.occurrence_id == occ["id"])
        assert reminder.due_at_utc.replace(tzinfo=UTC) == new_start - timedelta(minutes=60)

    async def test_moved_occurrence_survives_an_all_edit(self, client, fake_discord):
        event = await _create_event(client)
        occ = (await _occurrences(client))[0]
        new_start = datetime.combine(_future(2), time(21, 0), tzinfo=UTC)
        await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"start_datetime_utc": new_start.isoformat()})
        await client.patch(f"{BASE}/events/{event['id']}", json={"start_time_utc": "18:00"})
        first, second = (await _occurrences(client))[:2]
        assert first["start_datetime_utc"] == new_start.isoformat()
        assert second["start_datetime_utc"].endswith("T18:00:00+00:00")

    async def test_message_override_applies_to_one_occurrence(self, client, fake_discord, sf):
        await _create_event(client, message="normal")
        await _post_everything(sf, fake_discord)
        occ = (await _occurrences(client))[0]
        resp = await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"message_override": "special"})
        assert resp.json()["message_override"] == "special"
        assert next(c for c in fake_discord.calls if c[0] == "update")[4] == "special"
        cleared = await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"message_override": ""})
        assert cleared.json()["message_override"] is None

    async def test_cancel_and_move_together_is_rejected(self, client, fake_discord):
        await _create_event(client)
        occ = (await _occurrences(client))[0]
        resp = await client.patch(f"{BASE}/occurrences/{occ['id']}", json={
            "cancelled": True, "start_datetime_utc": datetime.now(UTC).isoformat()})
        assert resp.status_code == 422

    async def test_unknown_occurrence_is_404(self, client, fake_discord):
        assert (await client.patch(f"{BASE}/occurrences/9999", json={"cancelled": True})).status_code == 404

    async def test_discord_failure_on_cancel_is_reported_but_the_cancel_stands(self, client, fake_discord, sf):
        await _create_event(client)
        await _post_everything(sf, fake_discord)
        fake_discord.cancel_error = "Unknown Scheduled Event"
        occ = (await _occurrences(client))[0]
        resp = await client.patch(f"{BASE}/occurrences/{occ['id']}", json={"cancelled": True})
        assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
        assert resp.json()["discord_errors"] == ["Unknown Scheduled Event"]


class TestAllScope:
    async def test_editing_the_name_updates_posted_discord_events(self, client, fake_discord, sf):
        event = await _create_event(client)
        await _post_everything(sf, fake_discord)
        resp = await client.patch(f"{BASE}/events/{event['id']}", json={"name": "Grizzly Hunt"})
        assert resp.status_code == 200
        updates = [c for c in fake_discord.calls if c[0] == "update"]
        assert len(updates) == 4 and {u[3] for u in updates} == {"Grizzly Hunt"}

    async def test_deactivating_removes_posted_discord_events_and_pending_work(self, client, fake_discord, sf):
        event = await _create_event(client)
        await _post_everything(sf, fake_discord)
        await client.patch(f"{BASE}/events/{event['id']}", json={"active": False})
        assert fake_discord.count("cancel") == 4
        assert [d for d in await deliveries(sf) if d.status in ("pending", "posted")] == []

    async def test_deleting_removes_posted_discord_events(self, client, fake_discord, sf):
        event = await _create_event(client)
        await _post_everything(sf, fake_discord)
        assert (await client.delete(f"{BASE}/events/{event['id']}")).status_code == 204
        assert fake_discord.count("cancel") == 4
        assert await deliveries(sf) == []

    async def test_adding_a_reminder_adds_deliveries(self, client, fake_discord, sf):
        event = await _create_event(client)
        await client.patch(f"{BASE}/events/{event['id']}", json={"reminder_minutes": [1440, 60]})
        rows = await deliveries(sf, kind="reminder")
        assert sorted({d.reminder_minutes for d in rows}) == [60, 1440]


class TestSplit:
    async def test_this_and_following(self, client, fake_discord, sf):
        event = await _create_event(client)
        await _post_everything(sf, fake_discord)
        occs = await _occurrences(client)
        from_date = occs[2]["occurrence_date"]  # third occurrence
        resp = await client.post(f"{BASE}/events/{event['id']}/split", json={
            "from_date": from_date, "changes": {"start_time_utc": "20:30", "name": "Bear Hunt (late)"}})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["original"]["until_date"] == str(date.fromisoformat(from_date) - timedelta(days=1))
        assert body["created"]["series_id"] == body["original"]["series_id"]
        assert body["created"]["anchor_date"] == from_date
        assert body["created"]["start_time_utc"] == "20:30" and body["created"]["name"] == "Bear Hunt (late)"
        assert body["created"]["reminder_minutes"] == [60]

        after = await _occurrences(client)
        old = [o for o in after if o["event_id"] == event["id"]]
        new = [o for o in after if o["event_id"] == body["created"]["id"]]
        # Later occurrences that had already posted stay as cancelled history.
        assert [o["occurrence_date"] for o in old if o["status"] == "scheduled"] == [
            o["occurrence_date"] for o in occs[:2]]
        assert {o["status"] for o in old[2:]} == {"cancelled"}
        assert [o["occurrence_date"] for o in new] == [o["occurrence_date"] for o in occs[2:]]
        # the two later Discord events of the old series were removed
        assert fake_discord.count("cancel") == 2

    async def test_split_date_must_be_a_real_occurrence(self, client, fake_discord):
        event = await _create_event(client)
        bad = _future(2) + timedelta(days=8)
        resp = await client.post(f"{BASE}/events/{event['id']}/split", json={"from_date": str(bad)})
        assert resp.status_code == 422

    async def test_split_on_the_first_occurrence_is_rejected(self, client, fake_discord):
        event = await _create_event(client)
        resp = await client.post(f"{BASE}/events/{event['id']}/split", json={"from_date": event["anchor_date"]})
        assert resp.status_code == 422 and "all occurrences" in resp.json()["detail"]

    async def test_one_off_event_cannot_be_split(self, client, fake_discord):
        event = await _create_event(client, recurrence_kind="none", interval_days=None)
        resp = await client.post(f"{BASE}/events/{event['id']}/split", json={"from_date": str(_future(5))})
        assert resp.status_code == 422


# ----------------------------------------------------- delivery log and retry

class TestDeliveryLog:
    async def test_log_filters_and_health(self, client, fake_discord, sf, configured):
        fake_discord.create_error = "boom"
        await _create_event(client, reminder_minutes=[])
        await _post_everything(sf, fake_discord)
        errors = (await client.get(f"{BASE}/deliveries", params={"status": "error", "days": 30})).json()
        assert len(errors) == 4 and errors[0]["tenant_slug"] == "mod" and errors[0]["detail"] == "boom"
        assert (await client.get(f"{BASE}/deliveries", params={"status": "posted", "days": 30})).json() == []
        health = (await client.get(f"{BASE}/delivery-health")).json()
        assert health["counts"].get("error") is None or isinstance(health["counts"], dict)
        assert health["engine_enabled"] is False

    async def test_retry_only_applies_to_errors(self, client, fake_discord, sf):
        await _create_event(client)
        await _post_everything(sf, fake_discord)
        posted = (await client.get(f"{BASE}/deliveries", params={"status": "posted", "days": 30})).json()[0]
        assert (await client.post(f"{BASE}/deliveries/{posted['id']}/retry")).status_code == 409

    async def test_retry_puts_an_error_back_in_the_queue(self, client, fake_discord, sf):
        fake_discord.create_error = "boom"
        await _create_event(client, reminder_minutes=[])
        await _post_everything(sf, fake_discord)
        failed = (await client.get(f"{BASE}/deliveries", params={"status": "error", "days": 30})).json()[0]
        fake_discord.create_error = ""
        resp = await client.post(f"{BASE}/deliveries/{failed['id']}/retry")
        assert resp.status_code == 200 and resp.json()["status"] == "pending"
        await _post_everything(sf, fake_discord)
        rows = (await client.get(f"{BASE}/deliveries", params={"status": "posted", "days": 30})).json()
        assert failed["id"] in {r["id"] for r in rows}

    async def test_health_flags_a_stalled_tick(self, client, fake_discord, sf):
        await _create_event(client, reminder_minutes=[])
        health = (await client.get(f"{BASE}/delivery-health")).json()
        assert health["oldest_pending_overdue_minutes"] > 5 and health["healthy"] is False

    async def test_viewer_cannot_retry(self, make_user_and_client, client, fake_discord, sf, tenant):
        fake_discord.create_error = "boom"
        await _create_event(client, reminder_minutes=[])
        await _post_everything(sf, fake_discord)
        failed = (await client.get(f"{BASE}/deliveries", params={"status": "error", "days": 30})).json()[0]
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        viewer.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await viewer.post(f"{BASE}/deliveries/{failed['id']}/retry")).status_code == 403
