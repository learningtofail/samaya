"""A message per reminder (spec §77): API, copy on split, and what the engine sends."""
import json
from datetime import datetime

from models.db import AuditLog, Event, EventAlliance, EventOccurrence, EventReminder
from services.event_engine import run_delivery_tick
from sqlalchemy import select

from tests.test_unified_events import BASE, _event_body, _make_type
from tests.unified_helpers import UTC, make_event, sync

AT = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)  # 30 seconds after the 60 minute reminder is due


async def _created(client, **overrides):
    etype = await _make_type(client, default_message="base message")
    body = _event_body(etype["id"], **overrides)
    resp = await client.post(f"{BASE}/events", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _set_reminder_message(sf, event_id, minutes, text):
    async with sf() as s:
        row = (await s.execute(select(EventReminder).where(
            EventReminder.event_id == event_id, EventReminder.minutes_before == minutes))).scalar_one()
        row.message = text
        await s.commit()


async def _sent(fake):
    return [c[2] for c in fake.calls if c[0] == "send"]


class TestApi:
    async def test_create_with_messages_and_read_back(self, client):
        e = await _created(client, reminder_minutes=[60, 0], reminder_messages={"60": "One hour left", "0": "Now!"})
        assert e["reminder_minutes"] == [60, 0]
        assert e["reminder_messages"] == {"60": "One hour left", "0": "Now!"}

    async def test_no_messages_means_an_empty_map(self, client):
        assert (await _created(client, reminder_minutes=[60]))["reminder_messages"] == {}

    async def test_blank_text_is_dropped(self, client):
        e = await _created(client, reminder_minutes=[60, 0], reminder_messages={"60": "   ", "0": "Now!"})
        assert e["reminder_messages"] == {"0": "Now!"}

    async def test_message_for_a_missing_reminder_is_rejected(self, client):
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(
            etype["id"], reminder_minutes=[60], reminder_messages={"15": "Nope"}))
        assert resp.status_code == 422 and "15 minutes" in resp.text

    async def test_bad_keys_and_values_are_rejected(self, client):
        etype = await _make_type(client)
        for bad in ({"abc": "x"}, {"-5": "x"}, {"60": 5}, {"60": "x" * 2001}):
            resp = await client.post(f"{BASE}/events", json=_event_body(
                etype["id"], reminder_minutes=[60], reminder_messages=bad))
            assert resp.status_code == 422, bad

    async def test_patch_messages_alone_replaces_the_map(self, client):
        e = await _created(client, reminder_minutes=[60, 0], reminder_messages={"60": "A", "0": "B"})
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"reminder_messages": {"0": "B2"}})).json()
        assert out["reminder_messages"] == {"0": "B2"} and out["reminder_minutes"] == [60, 0]

    async def test_patch_minutes_alone_keeps_messages_of_kept_reminders(self, client):
        e = await _created(client, reminder_minutes=[60, 0], reminder_messages={"60": "A", "0": "B"})
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"reminder_minutes": [60, 15]})).json()
        assert out["reminder_minutes"] == [60, 15] and out["reminder_messages"] == {"60": "A"}

    async def test_patch_can_clear_every_message(self, client):
        e = await _created(client, reminder_minutes=[60], reminder_messages={"60": "A"})
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"reminder_messages": {}})).json()
        assert out["reminder_messages"] == {}

    async def test_patch_message_must_match_the_final_reminders(self, client):
        e = await _created(client, reminder_minutes=[60, 0])
        resp = await client.patch(f"{BASE}/events/{e['id']}", json={"reminder_minutes": [60], "reminder_messages": {"0": "x"}})
        assert resp.status_code == 422

    async def test_change_is_audited(self, client, db_session):
        e = await _created(client, reminder_minutes=[60])
        await client.patch(f"{BASE}/events/{e['id']}", json={"reminder_messages": {"60": "Hello"}})
        rows = (await db_session.execute(select(AuditLog).where(
            AuditLog.table_name == "events", AuditLog.row_id == e["id"], AuditLog.action == "update"))).scalars().all()
        assert json.loads(rows[-1].after)["reminder_messages"] == {"60": "Hello"}

    async def test_messages_never_reach_the_public_api(self, client, client_no_session):
        await _created(client, reminder_minutes=[60], reminder_messages={"60": "SECRET-REMINDER-TEXT"})
        assert "SECRET-REMINDER-TEXT" not in (await client_no_session.get("/api/events")).text


class TestEngine:
    async def test_reminder_uses_its_own_message(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", reminders=(60, 0), interval=None)
        await _set_reminder_message(sf, event_id, 60, "One hour to go")
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert await _sent(fake) == ["One hour to go"]

    async def test_reminder_without_a_message_uses_the_event_message(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", reminders=(60, 0), interval=None)
        await _set_reminder_message(sf, event_id, 0, "Starting now")
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert await _sent(fake) == ["event message"]

    async def test_each_reminder_sends_its_own_text(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", reminders=(60, 0), interval=None)
        await _set_reminder_message(sf, event_id, 60, "One hour")
        await _set_reminder_message(sf, event_id, 0, "Now")
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 19, 0, 30, tzinfo=UTC))
        assert await _sent(fake) == ["One hour", "Now"]

    async def test_reminder_message_beats_an_alliance_override(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", interval=None)
        async with sf() as s:
            row = (await s.execute(select(EventAlliance).where(EventAlliance.event_id == event_id))).scalar_one()
            row.message_override = "alliance message"
            await s.commit()
        await _set_reminder_message(sf, event_id, 60, "reminder message")
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert await _sent(fake) == ["reminder message"]

    async def test_alliance_override_still_applies_to_a_reminder_without_a_message(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", interval=None)
        async with sf() as s:
            row = (await s.execute(select(EventAlliance).where(EventAlliance.event_id == event_id))).scalar_one()
            row.message_override = "alliance message"
            await s.commit()
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert await _sent(fake) == ["alliance message"]

    async def test_occurrence_override_beats_the_reminder_message(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", interval=None)
        await _set_reminder_message(sf, event_id, 60, "reminder message")
        await sync(sf, event_id)
        async with sf() as s:
            occ = (await s.execute(select(EventOccurrence).where(EventOccurrence.event_id == event_id))).scalar_one()
            occ.message_override = "occurrence message"
            await s.commit()
        await run_delivery_tick(sf, fake, AT)
        assert await _sent(fake) == ["occurrence message"]

    async def test_placeholders_render_in_a_reminder_message(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None)
        await _set_reminder_message(sf, event_id, 60, "Starts {event_time_relative}")
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert (await _sent(fake))[0].startswith("Starts <t:")


class TestSplit:
    async def test_split_copies_reminder_messages(self, client, sf, db_session):
        e = await _created(client, reminder_minutes=[60, 0], reminder_messages={"60": "A", "0": "B"}, anchor_date="2026-10-05")
        resp = await client.post(f"{BASE}/events/{e['id']}/split", json={"from_date": "2026-10-19"})
        assert resp.status_code in (200, 201), resp.text
        events = (await db_session.execute(select(Event).where(Event.id != e["id"]))).scalars().all()
        new = next(ev for ev in events if str(ev.anchor_date) == "2026-10-19")
        async with sf() as s:
            rows = (await s.execute(select(EventReminder).where(EventReminder.event_id == new.id))).scalars().all()
        assert {r.minutes_before: r.message for r in rows} == {60: "A", 0: "B"}


class TestAtStartReminder:
    async def test_zero_minute_reminder_posts_at_the_start(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", reminders=(0,), interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 19, 0, 0, tzinfo=UTC))
        assert await _sent(fake) == ["event message"]

    async def test_zero_minute_reminder_is_dropped_when_far_too_late(self, sf, fake, configured):
        event_id = await make_event(sf, configured, reminders=(0,), interval=None)
        await sync(sf, event_id)
        counts = await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 19, 6, 0, tzinfo=UTC))
        assert await _sent(fake) == [] and counts.get("cancelled")

    async def test_earlier_reminders_are_still_dropped_once_started(self, sf, fake, configured):
        event_id = await make_event(sf, configured, reminders=(60,), interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 19, 0, 0, tzinfo=UTC))
        assert await _sent(fake) == []

    async def test_split_can_change_the_messages_of_the_new_part_only(self, client, sf, db_session):
        e = await _created(client, reminder_minutes=[60], reminder_messages={"60": "old"}, anchor_date="2026-10-05")
        resp = await client.post(f"{BASE}/events/{e['id']}/split", json={
            "from_date": "2026-10-19", "changes": {"reminder_messages": {"60": "new"}}})
        assert resp.status_code in (200, 201), resp.text
        events = (await db_session.execute(select(Event))).scalars().all()
        async with sf() as s:
            messages = {
                str(ev.anchor_date): (await s.execute(select(EventReminder.message).where(EventReminder.event_id == ev.id))).scalar_one()
                for ev in events}
        assert messages["2026-10-05"] == "old" and messages["2026-10-19"] == "new"


class TestDefaultText:
    async def test_each_reminder_says_its_own_time_until_the_event(self, sf, fake, configured):
        event_id = await make_event(sf, configured, reminders=(60, 0), interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 19, 0, 30, tzinfo=UTC))
        assert await _sent(fake) == ["1 hour until Bear Hunt", "Bear Hunt is starting now"]

    async def test_role_mention_still_leads_the_default_text(self, sf, fake, configured):
        event_id = await make_event(sf, configured, mention_role=True, interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert await _sent(fake) == ["<@&role-mod> 1 hour until Bear Hunt"]

    async def test_event_message_still_wins_over_the_default(self, sf, fake, configured):
        event_id = await make_event(sf, configured, message="event message", interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert await _sent(fake) == ["event message"]
