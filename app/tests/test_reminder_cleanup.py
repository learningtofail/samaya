"""Spec §83: self-cleaning reminders. The id is stored, earlier reminders go when
a later one posts, all go after the event, and a failure never blocks sending."""
from datetime import datetime, timedelta

from sqlalchemy import select, update

from models.db import Delivery, Event
from services.event_engine import CLEANUP_GIVE_UP_AFTER, run_cleanup, run_delivery_tick
from tests.unified_helpers import UTC, add_audience, deliveries, make_event, sync

A = "/admin/api"


def _t(hour, minute=0, second=30, day=2):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


T60, T15, T0 = _t(18, 0), _t(18, 45), _t(19, 0)
AFTER_END = _t(21, 1)  # the event runs 19:00 to 21:00


async def _event(sf, tenant, *, clean=True, reminders=(60, 15, 0), **kw):
    event_id = await make_event(sf, tenant, interval=None, duration=2.0, reminders=reminders, **kw)
    async with sf() as s:
        await s.execute(update(Event).where(Event.id == event_id).values(clean_up_reminders=clean))
        await s.commit()
    await sync(sf, event_id)
    return event_id


async def _reminders(sf, event_id, **filters):
    return {d.reminder_minutes: d for d in await deliveries(sf, event_id, kind="reminder", **filters)}


def _alive(fake, channel="chan-mod"):
    return sorted(text for (chan, _), text in fake.messages.items() if chan == channel)


class TestIds:
    async def test_every_posted_reminder_keeps_its_message_id(self, sf, fake, configured):
        event_id = await _event(sf, configured, clean=False)
        await run_delivery_tick(sf, fake, T60)
        row = (await _reminders(sf, event_id))[60]
        assert row.status == "posted" and row.discord_message_id in {mid for _, mid in fake.messages}

    async def test_a_failed_reminder_has_no_id(self, sf, fake, configured):
        fake.send_error = "500 Discord server error"
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        assert (await _reminders(sf, event_id))[60].discord_message_id is None


class TestRules:
    async def test_a_later_reminder_removes_the_earlier_one(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        assert len(_alive(fake)) == 1
        counts = await run_delivery_tick(sf, fake, T15)
        rows = await _reminders(sf, event_id)
        assert counts.get("cleanup_deleted") == 1 and len(_alive(fake)) == 1
        assert rows[60].message_deleted_at is not None and rows[15].message_deleted_at is None

    async def test_only_the_newest_stays_after_several(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        for at in (T60, T15, T0):
            await run_delivery_tick(sf, fake, at)
        rows = await _reminders(sf, event_id)
        assert len(_alive(fake)) == 1
        assert rows[60].message_deleted_at and rows[15].message_deleted_at and rows[0].message_deleted_at is None

    async def test_all_go_after_the_event_ends(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        for at in (T60, T15, T0):
            await run_delivery_tick(sf, fake, at)
        await run_delivery_tick(sf, fake, _t(20, 59))
        assert len(_alive(fake)) == 1
        await run_delivery_tick(sf, fake, AFTER_END)
        assert fake.messages == {} and all(d.message_deleted_at for d in (await _reminders(sf, event_id)).values())

    async def test_an_event_without_a_duration_ends_at_its_start(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None, duration=None, reminders=(60,))
        async with sf() as s:
            await s.execute(update(Event).where(Event.id == event_id).values(clean_up_reminders=True))
            await s.commit()
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, T60)
        await run_delivery_tick(sf, fake, _t(18, 59))
        assert len(_alive(fake)) == 1
        await run_delivery_tick(sf, fake, _t(19, 1))
        assert fake.messages == {}

    async def test_the_option_off_deletes_nothing(self, sf, fake, configured):
        await _event(sf, configured, clean=False)
        for at in (T60, T15, T0, AFTER_END):
            await run_delivery_tick(sf, fake, at)
        assert fake.count("delete") == 0 and len(_alive(fake)) == 3

    async def test_turning_it_on_later_cleans_up_what_is_already_posted(self, sf, fake, configured):
        event_id = await _event(sf, configured, clean=False)
        for at in (T60, T15):
            await run_delivery_tick(sf, fake, at)
        async with sf() as s:
            await s.execute(update(Event).where(Event.id == event_id).values(clean_up_reminders=True))
            await s.commit()
        await run_delivery_tick(sf, fake, T15 + timedelta(minutes=1))
        assert len(_alive(fake)) == 1

    async def test_a_single_reminder_before_the_event_is_left_alone(self, sf, fake, configured):
        await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        await run_delivery_tick(sf, fake, T60 + timedelta(minutes=1))
        assert fake.count("delete") == 0 and len(_alive(fake)) == 1

    async def test_a_cancelled_occurrence_keeps_its_reminders_until_the_event_end(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        from models.db import EventOccurrence
        async with sf() as s:
            await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == event_id).values(status="cancelled"))
            await s.commit()
        await run_delivery_tick(sf, fake, _t(18, 30))
        assert len(_alive(fake)) == 1
        await run_delivery_tick(sf, fake, AFTER_END)
        assert fake.messages == {}

    async def test_it_never_touches_the_discord_scheduled_event(self, sf, fake, configured):
        await _event(sf, configured)
        for at in (T60, T15, T0, AFTER_END):
            await run_delivery_tick(sf, fake, at)
        assert fake.count("cancel") == 0

    async def test_each_channel_is_judged_on_its_own(self, sf, fake, configured, second_tenant):
        await add_audience(sf, second_tenant, "chan-nsr")
        event_id = await _event(sf, configured, scope="kingdom-wide", audience=[configured["id"], second_tenant["id"]])
        await run_delivery_tick(sf, fake, T60)
        assert len(_alive(fake)) == 1 and len(_alive(fake, "chan-nsr")) == 1
        fake.raise_on_send_to = {"chan-nsr"}
        await run_delivery_tick(sf, fake, T15)
        assert len(_alive(fake)) == 1 and len(_alive(fake, "chan-nsr")) == 1  # chan-nsr never got the 15-minute one
        rows = await _reminders(sf, event_id, tenant_id=second_tenant["id"])
        assert rows[60].message_deleted_at is None

    async def test_a_shared_channel_is_one_message_and_one_delete(self, sf, fake, tenant, second_tenant):
        await add_audience(sf, tenant, "chan-shared", label="Shared A")
        await add_audience(sf, second_tenant, "chan-shared", label="Shared B", server_id=tenant["server_id"])
        await _event(sf, tenant, scope="kingdom-wide", audience=[tenant["id"], second_tenant["id"]], reminders=(60, 15))
        for at in (T60, T15):
            await run_delivery_tick(sf, fake, at)
        assert fake.count("send") == 2 and fake.count("delete") == 1 and len(_alive(fake, "chan-shared")) == 1


class TestFailures:
    async def _two_posted(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        fake.delete_error = "403 Missing permissions"
        await run_delivery_tick(sf, fake, T15)
        return event_id

    async def test_a_message_already_gone_counts_as_deleted(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        fake.messages.clear()
        await run_delivery_tick(sf, fake, T15)
        assert (await _reminders(sf, event_id))[60].message_deleted_at is not None

    async def test_an_access_failure_is_stored_shown_and_not_retried(self, sf, fake, configured):
        event_id = await self._two_posted(sf, fake, configured)
        row = (await _reminders(sf, event_id))[60]
        assert row.cleanup_error.startswith("403") and row.message_deleted_at is None
        deletes = fake.count("delete")
        await run_delivery_tick(sf, fake, T15 + timedelta(minutes=1))
        await run_delivery_tick(sf, fake, AFTER_END)
        assert fake.count("delete") == deletes + 1  # only the 15-minute one, after the event; the 60-minute one is never retried

    async def test_the_failure_does_not_stop_the_later_reminder(self, sf, fake, configured):
        event_id = await self._two_posted(sf, fake, configured)
        assert (await _reminders(sf, event_id))[15].status == "posted"

    async def test_the_delivery_log_shows_the_cleanup_error_and_the_removal(self, sf, client, fake, configured):
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        fake.delete_error = "403 Missing permissions"
        await run_delivery_tick(sf, fake, T15)
        fake.delete_error = ""
        await run_delivery_tick(sf, fake, AFTER_END)
        rows = (await client.get(f"{A}/deliveries", params={"section": "past", "days": 30})).json()
        mine = {r["reminder_minutes"]: r for r in rows if r["event_id"] == event_id and r["kind"] == "reminder"}
        assert mine[60]["cleanup_error"].startswith("403") and mine[60]["message_deleted_at"] is None
        assert mine[15]["message_deleted_at"] is not None and mine[15]["cleanup_error"] is None

    async def test_a_transient_failure_is_retried_then_given_up(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)
        fake.delete_error = "429 Rate limited"
        await run_delivery_tick(sf, fake, T15)
        row = (await _reminders(sf, event_id))[60]
        assert row.message_deleted_at is None and row.cleanup_error is None
        await run_delivery_tick(sf, fake, T15 + timedelta(minutes=1))
        assert fake.count("delete") == 2
        fake.delete_error = ""
        too_late = _t(21, 0) + CLEANUP_GIVE_UP_AFTER + timedelta(minutes=1)
        before = fake.count("delete")
        counts = await run_cleanup(sf, fake, too_late)
        assert fake.count("delete") == before and counts == {"deleted": 0, "failed": 0, "retry": 0}

    async def test_a_delete_that_raises_does_not_stop_sending(self, sf, fake, configured):
        event_id = await _event(sf, configured)
        await run_delivery_tick(sf, fake, T60)

        async def boom(*args, **kwargs):
            raise RuntimeError("boom")
        fake.delete_channel_message = boom
        counts = await run_delivery_tick(sf, fake, T15)
        assert counts.get("posted") == 1 and counts.get("cleanup_retry") == 1
        assert (await _reminders(sf, event_id))[15].status == "posted"

    async def test_one_failing_delete_does_not_stop_the_others(self, sf, fake, configured, second_tenant):
        await add_audience(sf, second_tenant, "chan-nsr")
        await _event(sf, configured, scope="kingdom-wide", audience=[configured["id"], second_tenant["id"]])
        await run_delivery_tick(sf, fake, T60)
        original = fake.delete_channel_message

        async def flaky(token, channel_id, message_id):
            if channel_id == "chan-mod":
                return False, "403 Missing permissions"
            return await original(token, channel_id, message_id)
        fake.delete_channel_message = flaky
        await run_delivery_tick(sf, fake, T15)
        assert len(_alive(fake)) == 2 and len(_alive(fake, "chan-nsr")) == 1  # chan-mod's delete failed; chan-nsr's 60-minute one is gone
        async with sf() as s:
            errors = (await s.execute(select(Delivery.cleanup_error).where(Delivery.cleanup_error.is_not(None)))).scalars().all()
        assert len(errors) == 1


class TestApi:
    async def _type(self, client):
        return (await client.post(f"{A}/event-types", json={"name": "Major", "default_duration_hours": 2})).json()["id"]

    def _body(self, type_id, **kw):
        body = {"type_id": type_id, "name": "Bear Hunt", "start_time_utc": "19:00", "anchor_date": "2030-01-05",
                "reminder_minutes": [60, 15], "recurrence_kind": "interval_days", "interval_days": 7, "duration_hours": 2}
        body.update(kw)
        return body

    async def test_default_is_off_and_it_round_trips(self, client, tenant):
        type_id = await self._type(client)
        off = (await client.post(f"{A}/events", json=self._body(type_id))).json()
        on = (await client.post(f"{A}/events", json=self._body(type_id, name="Other", clean_up_reminders=True))).json()
        assert off["clean_up_reminders"] is False and on["clean_up_reminders"] is True
        listed = {e["id"]: e for e in (await client.get(f"{A}/events")).json()}
        assert listed[on["id"]]["clean_up_reminders"] is True

    async def test_patch_toggles_it_and_audits(self, client, tenant, sf):
        type_id = await self._type(client)
        event = (await client.post(f"{A}/events", json=self._body(type_id))).json()
        resp = await client.patch(f"{A}/events/{event['id']}", json={"clean_up_reminders": True})
        assert resp.status_code == 200 and resp.json()["clean_up_reminders"] is True
        resp = await client.patch(f"{A}/events/{event['id']}", json={"name": "Renamed"})
        assert resp.json()["clean_up_reminders"] is True
        from models.db import AuditLog
        async with sf() as s:
            rows = (await s.execute(select(AuditLog).where(AuditLog.table_name == "events", AuditLog.action == "update"))).scalars().all()
        assert any("clean_up_reminders" in (r.after or "") for r in rows)

    async def test_split_copies_the_flag(self, client, tenant, fake):
        from main import app
        from services.discord_client import get_discord
        app.dependency_overrides[get_discord] = lambda: fake
        type_id = await self._type(client)
        event = (await client.post(f"{A}/events", json=self._body(type_id, clean_up_reminders=True))).json()
        resp = await client.post(f"{A}/events/{event['id']}/split", json={"from_date": "2030-01-19", "changes": {"name": "Later"}})
        assert resp.status_code == 201, resp.text
        assert resp.json()["created"]["clean_up_reminders"] is True and resp.json()["original"]["clean_up_reminders"] is True

    async def test_a_viewer_cannot_change_it(self, client, make_user_and_client, tenant):
        type_id = await self._type(client)
        event = (await client.post(f"{A}/events", json=self._body(type_id))).json()
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")], kingdom_grants=[])
        viewer.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await viewer.patch(f"{A}/events/{event['id']}", json={"clean_up_reminders": True})).status_code == 403

    async def test_public_routes_do_not_expose_it(self, client, client_no_session, tenant):
        type_id = await self._type(client)
        await client.post(f"{A}/events", json=self._body(type_id, clean_up_reminders=True))
        for path in ("/api/events", "/events.ics"):
            assert "clean_up" not in (await client_no_session.get(path)).text
