"""Spec §81: a destination that keeps answering 403 or 404 is paused, skipped
without calling Discord, shown in the delivery health, and resumed by hand."""
from datetime import datetime, timedelta

from sqlalchemy import select

from models.db import AuditLog, AudienceDestination
from services.event_engine import PAUSE_AFTER_FAILURES, run_delivery_tick
from tests.unified_helpers import UTC, add_audience, deliveries, make_event, sync

A = "/admin/api"
DAY1 = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)


async def _destination(sf, channel="chan-mod"):
    async with sf() as s:
        return (await s.execute(select(AudienceDestination).where(AudienceDestination.channel_id == channel))).scalar_one()


async def _daily_event(sf, tenant, **kw):
    event_id = await make_event(sf, tenant, interval=1, duration=1.0, **kw)
    await sync(sf, event_id)
    return event_id


async def _tick_days(sf, fake, days):
    for d in days:
        await run_delivery_tick(sf, fake, DAY1 + timedelta(days=d))


class TestPauseRules:
    async def test_third_access_failure_pauses_the_destination(self, sf, fake, configured):
        fake.send_error = "403 Missing permissions"
        await _daily_event(sf, configured)
        await _tick_days(sf, fake, range(PAUSE_AFTER_FAILURES))
        dest = await _destination(sf)
        assert dest.paused_at is not None and dest.consecutive_failures == PAUSE_AFTER_FAILURES
        assert "403 Missing permissions" in dest.pause_reason

    async def test_fewer_failures_do_not_pause(self, sf, fake, configured):
        fake.send_error = "404 Channel not found"
        await _daily_event(sf, configured)
        await _tick_days(sf, fake, range(PAUSE_AFTER_FAILURES - 1))
        dest = await _destination(sf)
        assert dest.paused_at is None and dest.consecutive_failures == PAUSE_AFTER_FAILURES - 1

    async def test_a_successful_post_resets_the_count(self, sf, fake, configured):
        await _daily_event(sf, configured)
        fake.send_error = "403 Missing permissions"
        await _tick_days(sf, fake, range(2))
        fake.send_error = ""
        await _tick_days(sf, fake, [2])
        dest = await _destination(sf)
        assert dest.consecutive_failures == 0 and dest.paused_at is None

    async def test_other_errors_neither_count_nor_reset(self, sf, fake, configured):
        await _daily_event(sf, configured)
        fake.send_error = "403 Missing permissions"
        await _tick_days(sf, fake, range(2))
        for error in ("429 Rate limited", "Network error: timeout", "HTTP 500", "400 Bad request"):
            fake.send_error = error
            await _tick_days(sf, fake, [2])
        dest = await _destination(sf)
        assert dest.consecutive_failures == 2 and dest.paused_at is None

    async def test_paused_destination_is_not_called_and_not_counted(self, sf, fake, configured):
        fake.send_error = "403 Missing permissions"
        event_id = await _daily_event(sf, configured)
        await _tick_days(sf, fake, range(PAUSE_AFTER_FAILURES))
        sends = fake.count("send")
        await _tick_days(sf, fake, [PAUSE_AFTER_FAILURES, PAUSE_AFTER_FAILURES + 1])
        assert fake.count("send") == sends
        dest = await _destination(sf)
        assert dest.consecutive_failures == PAUSE_AFTER_FAILURES
        skipped = [d for d in await deliveries(sf, event_id, kind="reminder") if (d.detail or "").startswith("Paused: ")]
        assert len(skipped) == 2 and all(d.status == "error" for d in skipped)

    async def test_a_merged_channel_counts_one_failure_per_send(self, sf, fake, tenant, second_tenant):
        fake.send_error = "403 Missing permissions"
        await add_audience(sf, tenant, "chan-shared", label="Shared", also=())
        await add_audience(sf, second_tenant, "chan-shared", label="Shared 2", server_id=tenant["server_id"])
        event_id = await make_event(sf, tenant, scope="kingdom-wide", interval=1, duration=1.0,
                                    audience=[tenant["id"], second_tenant["id"]])
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, DAY1)
        assert fake.count("send") == 1
        async with sf() as s:
            rows = (await s.execute(select(AudienceDestination).where(AudienceDestination.channel_id == "chan-shared"))).scalars().all()
        assert sorted(r.consecutive_failures for r in rows) == [0, 1]

    async def test_another_destination_keeps_posting(self, sf, fake, tenant, second_tenant):
        await add_audience(sf, tenant, "chan-mod")
        await add_audience(sf, second_tenant, "chan-nsr")
        fake.raise_on_send_to = set()
        original = fake.send_channel_message

        async def flaky(token, channel, content):
            if channel == "chan-mod":
                fake.calls.append(("send", channel, content))
                return False, "403 Missing permissions"
            return await original(token, channel, content)
        fake.send_channel_message = flaky
        event_id = await make_event(sf, tenant, scope="kingdom-wide", interval=1, duration=1.0,
                                    audience=[tenant["id"], second_tenant["id"]])
        await sync(sf, event_id)
        await _tick_days(sf, fake, range(PAUSE_AFTER_FAILURES + 1))
        assert (await _destination(sf, "chan-mod")).paused_at is not None
        nsr = await _destination(sf, "chan-nsr")
        assert nsr.paused_at is None
        posted = [d for d in await deliveries(sf, event_id, kind="reminder", tenant_id=second_tenant["id"]) if d.status == "posted"]
        assert len(posted) == PAUSE_AFTER_FAILURES + 1


class TestResumeAndHealth:
    async def _pause(self, sf, fake, configured):
        fake.send_error = "403 Missing permissions"
        event_id = await _daily_event(sf, configured)
        await _tick_days(sf, fake, range(PAUSE_AFTER_FAILURES))
        return event_id, await _destination(sf)

    async def test_health_lists_the_paused_destination(self, client, sf, fake, configured):
        _, dest = await self._pause(sf, fake, configured)
        health = (await client.get(f"{A}/delivery-health")).json()
        assert health["healthy"] is False
        assert [p["id"] for p in health["paused_destinations"]] == [dest.id]
        assert health["paused_destinations"][0]["slug"] == configured["slug"]

    async def test_health_is_empty_when_nothing_is_paused(self, client, sf, configured):
        assert (await client.get(f"{A}/delivery-health")).json()["paused_destinations"] == []

    async def test_audiences_show_the_pause(self, client, sf, fake, configured):
        await self._pause(sf, fake, configured)
        audiences = (await client.get(f"{A}/audiences")).json()
        destinations = [d for a in audiences for d in a["destinations"]]
        assert destinations[0]["paused_at"] is not None and "403" in destinations[0]["pause_reason"]

    async def test_resume_clears_the_pause_and_retry_posts(self, client, sf, fake, configured):
        event_id, dest = await self._pause(sf, fake, configured)
        await _tick_days(sf, fake, [PAUSE_AFTER_FAILURES])
        skipped = [d for d in await deliveries(sf, event_id, kind="reminder") if (d.detail or "").startswith("Paused: ")][0]
        resp = await client.post(f"{A}/audience-destinations/{dest.id}/resume")
        assert resp.status_code == 200 and resp.json()["paused_at"] is None
        after = await _destination(sf)
        assert after.paused_at is None and after.consecutive_failures == 0 and after.pause_reason is None
        fake.send_error = ""
        assert (await client.post(f"{A}/deliveries/{skipped.id}/retry")).status_code == 200
        await run_delivery_tick(sf, fake, DAY1 + timedelta(days=PAUSE_AFTER_FAILURES, minutes=1))
        retried = [d for d in await deliveries(sf, event_id, kind="reminder") if d.id == skipped.id][0]
        assert retried.status == "posted"

    async def test_resume_is_audited(self, client, sf, fake, configured):
        _, dest = await self._pause(sf, fake, configured)
        await client.post(f"{A}/audience-destinations/{dest.id}/resume")
        async with sf() as s:
            rows = (await s.execute(select(AuditLog).where(
                AuditLog.table_name == "audience_destinations", AuditLog.row_id == dest.id))).scalars().all()
        assert len(rows) == 1 and rows[0].action == "update"

    async def test_resuming_an_unpaused_destination_is_a_no_op(self, client, sf, configured):
        dest = await _destination(sf)
        assert (await client.post(f"{A}/audience-destinations/{dest.id}/resume")).status_code == 200
        async with sf() as s:
            assert (await s.execute(select(AuditLog).where(AuditLog.table_name == "audience_destinations"))).first() is None

    async def test_viewer_and_non_coordinator_cannot_resume(self, make_user_and_client, sf, fake, configured, tenant):
        _, dest = await self._pause(sf, fake, configured)
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")], kingdom_grants=[tenant["kingdom_id"]])
        viewer.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await viewer.post(f"{A}/audience-destinations/{dest.id}/resume")).status_code == 403
        owner, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")], kingdom_grants=[],
                                              discord_id="owner-no-kingdom")
        owner.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await owner.post(f"{A}/audience-destinations/{dest.id}/resume")).status_code == 403
        assert (await _destination(sf)).paused_at is not None

    async def test_unknown_destination_is_404(self, client, configured):
        assert (await client.post(f"{A}/audience-destinations/99999/resume")).status_code == 404

    async def test_other_kingdoms_destination_is_404(self, client, sf, fake, configured):
        from models.db import Audience, DiscordServer, Kingdom
        await self._pause(sf, fake, configured)
        async with sf() as s:
            k = Kingdom(name="Other", slug="other")
            s.add(k)
            await s.flush()
            srv = DiscordServer(kingdom_id=k.id, name="X", guild_id="g-x")
            s.add(srv)
            await s.flush()
            a = Audience(kingdom_id=k.id, label="Elsewhere")
            a.destinations = [AudienceDestination(server_id=srv.id, channel_id="c-x")]
            s.add(a)
            await s.commit()
            other_id = a.destinations[0].id
        assert (await client.post(f"{A}/audience-destinations/{other_id}/resume")).status_code == 404

    async def test_public_routes_say_nothing_about_pauses(self, client_no_session, sf, fake, configured):
        await self._pause(sf, fake, configured)
        for path in ("/api/events", "/api/alliances", "/api/kingdom-branding"):
            body = (await client_no_session.get(path)).text
            assert "pause" not in body.lower() and "403" not in body
